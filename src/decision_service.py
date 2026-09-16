"""Bounded, persisted meal discovery. Model availability never blocks a choice."""

from __future__ import annotations

import copy
import re
import time
import uuid
from collections import Counter
from typing import Any

from decision_catalog import eligible, menu_choices, service_rate
from decision_preferences import (
    PERSISTENT_FIELDS,
    SOFT_LABELS,
    meal_wishes,
    split_message,
)
from observability import emit
from preference_engine import (
    ALLERGEN_ALIASES,
    DIETARY_TERMS,
    merge_preference_delta,
    parse_preferences,
)

MAX_QUESTIONS = 2
START_PATTERN = re.compile(
    r"不知道.*吃|不知.*吃|吃什麼好|吃甚麼好|幫我選|幫我挑|選不出|選擇障礙|隨便吃"
    r"|幫我決定|直接推薦|不用問了"
)


class StaleDecision(ValueError):
    pass


def starts_discovery(text: str) -> bool:
    return bool(START_PATTERN.search(text))


def _longest_common_run(one: str, other: str) -> int:
    best = 0
    previous = [0] * (len(other) + 1)
    for index in range(1, len(one) + 1):
        current = [0] * (len(other) + 1)
        for other_index in range(1, len(other) + 1):
            if one[index - 1] == other[other_index - 1]:
                current[other_index] = previous[other_index - 1] + 1
                best = max(best, current[other_index])
        previous = current
    return best


def _related_family(one: str, other: str) -> bool:
    """同一道菜的修飾版。「玫瑰霜降牛小排」和「霜降牛小排切厚切」互不包含，
    但共用「霜降牛小排」這段核心；「雞肉飯」和「雞肉湯麵」只共用兩個字，不算。"""
    if not one or not other:
        return False
    if one in other or other in one:
        return True
    shared = _longest_common_run(one, other)
    return shared >= 4 and shared / min(len(one), len(other)) >= 0.6


def _option(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _constraints(prefs: dict[str, Any]) -> list[str]:
    labels = []
    if prefs.get("budget") is not None:
        labels.append(f"每份 $ {prefs['budget']:g} 內")
    for field in ("dishType", "texture", "protein", "spiceLevel"):
        if prefs.get(field):
            labels.append(str(prefs[field]))
    for field, values in SOFT_LABELS.items():
        if prefs.get(field) in values:
            labels.append(values[prefs[field]])
    labels.extend(f"避開{term}" for term in prefs.get("excludes", []))
    labels.extend(f"過敏原：{ALLERGEN_ALIASES.get(term, (term,))[0]}"
                  for term in prefs.get("allergens", []))
    labels.extend(f"飲食限制：{DIETARY_TERMS.get(term, (term,))[0]}"
                  for term in prefs.get("dietaryRestrictions", []))
    if prefs.get("cheaperThan") is not None:
        labels.append(f"低於 $ {prefs['cheaperThan']:g}")
    labels.extend(f"這餐先不吃{term}" for term in prefs.get("rejectedTypes", []))
    return labels


class DecisionService:
    def __init__(self, sessions, menus, default_restaurant):
        self.sessions = sessions
        self.menus = menus
        self.default_restaurant = default_restaurant

    def _context(self, session_id):
        state = self.sessions.get(session_id, self.default_restaurant)
        menu = self.menus.get(state.active_restaurant or "")
        if menu is None:
            raise LookupError("請先選擇餐廳或上傳菜單，再開始選餐。")
        return state, menu

    def current(self, session_id: str) -> dict[str, Any] | None:
        state, menu = self._context(session_id)
        with state.lock:
            if not state.decision:
                return None
            _, fingerprint = menu_choices(menu)
            if (
                state.decision.get("restaurant") != state.active_restaurant
                or state.decision.get("menuVersion") != fingerprint
            ):
                state.decision = {}
                self.sessions.save(session_id)
                return None
            return copy.deepcopy(state.decision.get("view"))

    def active(self, session_id: str) -> bool:
        return bool(self.sessions.get(session_id, self.default_restaurant).decision)

    def accepts_message(self, session_id: str, text: str) -> bool:
        """Only consume selection commands; questions must remain available to AI."""
        text, _ = split_message(text)
        if not text:
            return False
        if starts_discovery(text) and not re.search(r"為什麼|為何|怎麼會|解釋", text):
            return True
        if not self.current(session_id):
            return False
        state = self.sessions.get(session_id, self.default_restaurant)
        with state.lock:
            decision = copy.deepcopy(state.decision)
        action, _, _ = self._text_action(decision, text)
        if action != "message":
            return True
        # Probe a copy so routing never changes preferences or card revisions.
        if self._apply_text(decision, text):
            return True
        # An already-satisfied request (e.g. "改成麵" twice) is still a meal action.
        return self._apply_text({"prefs": {}, "asked": [], "question": None}, text)

    def handle(
        self,
        session_id: str,
        action: str = "message",
        text: str = "",
        revision: str | None = None,
        value: str | None = None,
        item_id: str | None = None,
        reason: str = "another",
    ) -> dict[str, Any]:
        state, menu = self._context(session_id)
        with state.lock:
            rows, fingerprint = menu_choices(menu)
            old = state.decision
            valid = bool(
                old
                and old.get("restaurant") == state.active_restaurant
                and old.get("menuVersion") == fingerprint
            )
            if action not in {"start", "message"} and (
                not valid or not revision or revision != old.get("revision")
            ):
                raise StaleDecision("選餐內容已更新，請使用最新的選項。")
            if action == "message" and revision and (not valid or revision != old.get("revision")):
                raise StaleDecision("選餐內容已更新，請重新送出這次需求。")
            if action == "start" or not valid:
                prefs = {
                    key: copy.deepcopy(state.prefs[key])
                    for key in (
                        "budget",
                        "excludes",
                        "likes",
                        "allergens",
                        "dietaryRestrictions",
                        "spiceProfile",
                        "spiceLevel",
                    )
                    if key in state.prefs
                }
                d = self._new(state.active_restaurant, fingerprint, prefs)
            else:
                d = copy.deepcopy(old)
            stable_source = {**state.prefs, **{
                key: d["prefs"][key] for key in ("allergens", "dietaryRestrictions")
                if key in d["prefs"]
            }}
            d.setdefault("persistentPrefs", {
                key: copy.deepcopy(stable_source[key])
                for key in PERSISTENT_FIELDS if stable_source.get(key)
            })
            if action == "reset":
                d = self._new(
                    state.active_restaurant, fingerprint, copy.deepcopy(d["persistentPrefs"])
                )
            rate = service_rate(menu, state.active_restaurant or "")
            notice = (
                "開始新的一餐，已保留過敏與固定忌口，這餐的預算和口味可以重新選。"
                if action == "reset" and d["persistentPrefs"] else ""
            )
            force = action in {
                "recommend",
                "replace",
                "reject_all",
                "review",
                "reconsider",
                "relax_budget",
                "relax_type",
                "decide",
            }
            keep_ids: list[str] = []
            if action == "decide":
                d["focused"] = True
            elif action in {"recommend", "reconsider"}:
                d["focused"] = False
            textual_action = action in {"start", "message", "reset"}
            text_action, text_value, text_item_id = (
                self._text_action(d, text) if textual_action else ("message", None, None)
            )
            if action == "answer":
                self._answer(d, value)
            # Every path consumes the same preference delta exactly once, before filtering
            # or confirming. Exact option answers are handled by _answer instead.
            changed = (
                self._apply_text(d, text) if text.strip() and text_action != "answer" else False
            )
            if action == "relax_budget":
                d["prefs"].pop("budget", None)
                d["prefs"].pop("cheaperThan", None)
                notice = "已依你的選擇移除價格上限，其他條件保留。"
            elif action == "relax_type":
                for key in ("dishType", "texture", "protein", "rejectedTypes"):
                    d["prefs"].pop(key, None)
                notice = "已放寬餐點類型，預算與忌口保留。"
            elif action == "review":
                d["rejected"] = []
                d["exactRejections"] = []
                notice = "重新看看先前略過的餐點，其他條件保留。"
            elif action == "replace":
                target = self._displayed(d, item_id)
                if reason not in {"another", "price", "type", "recent", "light", "portion"}:
                    raise ValueError("無法辨識替換原因")
                d["rejected"] = list(dict.fromkeys([*d["rejected"], target["id"]]))
                d["rounds"] = d.get("rounds", 0) + 1
                if reason == "portion":
                    d["exactRejections"] = [*d.get("exactRejections", []), target["id"]]
                d["feedback"] = [*d["feedback"], {"id": target["id"], "reason": reason}][-100:]
                if reason == "price" and target.get("total") is not None:
                    previous = d["prefs"].get("cheaperThan", target["total"])
                    d["prefs"]["cheaperThan"] = min(previous, target["total"])
                    notice = "這次找更便宜的，其他需求保留。"
                elif reason == "type" and target.get("dishType"):
                    d["prefs"]["rejectedTypes"] = list(
                        dict.fromkeys(
                            [
                                *d["prefs"].get("rejectedTypes", []),
                                target["dishType"],
                            ]
                        )
                    )
                    if d["prefs"].get("dishType") == target["dishType"]:
                        d["prefs"].pop("dishType", None)
                    notice = f"這一餐先避開{target['dishType']}，其他需求保留。"
                elif reason in {"light", "portion"}:
                    d["prefs"]["taste" if reason == "light" else "portion"] = (
                        "light" if reason == "light" else "large"
                    )
                    notice = "換個更清爽的方向，預算與忌口保留。" if reason == "light" else (
                        "優先找菜單標示大份的餐點，預算與忌口保留。"
                    )
                else:
                    notice = "先略過這一道，其他需求保留。"
                keep_ids = [item["id"] for item in d["displayed"] if item["id"] != item_id]
            elif action == "reject_all":
                notice = self._reject_with_reason(d, reason)
            elif action == "choose":
                self._displayed(d, item_id)
                if item_id in {r["id"] for r in eligible(rows, d["prefs"], rate)}:
                    return self._choose(state, session_id, d, item_id, text)
                force = True
                notice = "這道不符合你剛補充的限制，先改列符合條件的餐點。"
            if text.strip() and textual_action:
                value, item_id = text_value, text_item_id
                if text_action == "answer":
                    self._answer(d, value)
                elif text_action == "choose":
                    if item_id in {r["id"] for r in eligible(rows, d["prefs"], rate)}:
                        return self._choose(state, session_id, d, item_id, text)
                    force = True
                    notice = "這道不符合你剛補充的限制，先改列符合條件的餐點。"
                elif text_action == "reject_all":
                    wishes, _ = meal_wishes(text)
                    notice = self._reject_with_reason(d, self._feedback_reason(text), wishes)
                    if wishes:
                        d["feedback"][-1]["wishes"] = wishes
                        notice = "依你剛才的需求換一批，預算與忌口保留。"
                    force = True
                elif text_action in {"recommend", "decide"}:
                    d["focused"] = text_action == "decide"
                    force = True
                elif text_action == "cheaper":
                    prices = [
                        item["total"] for item in d["displayed"] if item.get("total") is not None
                    ]
                    if prices:
                        d["prefs"]["cheaperThan"] = min(prices)
                    force = True
                    notice = "找比剛才更便宜的餐點，其他需求保留。"
                else:
                    if not changed and not starts_discovery(text):
                        notice = (
                            "這個描述還無法直接對應菜單。"
                            "可以點下方選項，或告訴我預算、飯麵偏好或忌口。"
                        )
                    force = force or old.get("phase") in {"recommendation", "no_match", "selection"}
            candidates = eligible(rows, d["prefs"], rate)
            rejected_families = {
                row["family"] for row in rows if row["id"] in d["rejected"]
                and row["id"] not in d.get("exactRejections", [])
            }
            available = [row for row in candidates if row["family"] not in rejected_families
                         and row["id"] not in d["rejected"]]
            gaps = [field for field in SOFT_LABELS if d["prefs"].get(field)
                    and available and not any(row.get(field) == d["prefs"][field]
                                              for row in available)]
            gap_labels = "、".join("口味" if field == "taste" else "份量" for field in gaps)
            d["dataNote"] = (
                f"目前候選缺少能確認符合你{gap_labels}需求的標示。"
                "先依品名、類型或預算縮小範圍，實際口味與份量需向店家確認。"
                if gaps else ""
            )
            new_wish_gap = any(d["prefs"].get(field) != old.get("prefs", {}).get(field)
                               for field in gaps)
            bypass_questions = action in {"decide", "recommend", "choose"} or text_action in {
                "decide", "recommend", "choose",
            }
            guide_with_evidence = new_wish_gap and not bypass_questions
            question = None
            if (
                ((not force and len(available) > 3)
                 or (guide_with_evidence and len(available) > 1))
                and (d.get("question") or len(d["asked"]) < MAX_QUESTIONS)
            ):
                question = self._next_question(d, available, allow_comparison=guide_with_evidence)
            if question:
                d["question"] = question
                if question["field"] not in d["asked"]:
                    d["asked"].append(question["field"])
                d["phase"] = "question"
                view = self._view(d, "question", notice or question["title"], question=question)
            else:
                d["question"] = None
                shortlist_id = d.pop("shortlistId", None)
                if shortlist_id:
                    keep_ids = [shortlist_id]
                cards = [
                    self._card(row, d["prefs"], rate) for row in self._pick(available, keep_ids)
                ]
                if d.get("focused"):
                    cards = cards[:1]
                d["displayed"] = cards
                d["phase"] = "recommendation" if cards else "no_match"
                if cards:
                    message = notice or "挑了幾個不同的選項，選一道你現在想吃的。"
                    if len(cards) == 1:
                        message = notice or "目前有這一道符合已知條件，可以先看看。"
                    if d.get("focused"):
                        message = notice or "我會先選這一道。願意的話就吃這個，也可以再換一個。"
                    view = self._view(d, "recommendation", message, items=cards)
                else:
                    exhausted = bool(candidates)
                    message = (
                        "符合條件的餐點都看過了。可以重新看看，或輸入新的需求。"
                        if exhausted
                        else "目前沒有符合這些條件的餐點。你可以調整條件，或換一家餐廳。"
                    )
                    # 整份菜單都沒標價時，預算會濾掉全部餐點。說成「條件太嚴」會害
                    # 使用者一直放寬其他條件，但那些條件根本不是原因。
                    if (
                        rows
                        and not exhausted
                        and any(k in d["prefs"] for k in ("budget", "cheaperThan"))
                        and not any(row["price"] is not None for row in rows)
                    ):
                        message = (
                            "這份菜單沒有標示價格，沒辦法用預算篩選。"
                            "可以移除價格上限改用其他條件，或先確認菜單價格。"
                        )
                    if not rows:
                        message = "這份菜單沒有可辨識的主餐，請先查看或修正菜單，也可以換一家餐廳。"
                    view = self._view(
                        d,
                        "no_match",
                        message,
                        exhausted=exhausted,
                        canRelaxBudget=any(k in d["prefs"] for k in ("budget", "cheaperThan")),
                        canRelaxType=any(
                            k in d["prefs"]
                            for k in ("dishType", "texture", "protein", "rejectedTypes")
                        ),
                    )
            label = {
                "start": "不知道吃什麼",
                "answer": str(value or "都可以"),
                "recommend": "直接推薦",
                "decide": "幫我決定一道",
                "replace": "換一道",
                "reject_all": "換一批",
                "reset": "開始新的一餐",
                "review": "重新看略過的餐點",
                "relax_budget": "移除價格上限",
                "relax_type": "放寬餐點類型",
                "reconsider": "再看看其他選項",
            }.get(action, "調整需求")
            return self._save(state, session_id, d, view, text or label)

    @staticmethod
    def _new(restaurant, fingerprint, prefs):
        return {
            "restaurant": restaurant,
            "menuVersion": fingerprint,
            "prefs": prefs,
            "asked": [],
            "rejected": [],
            "displayed": [],
            "feedback": [],
            "question": None,
            "phase": "question",
            "focused": False,
            "persistentPrefs": {
                key: copy.deepcopy(prefs[key]) for key in PERSISTENT_FIELDS if prefs.get(key)
            },
            "mealId": uuid.uuid4().hex,
            "startedAt": time.time(),
            "interactions": 0,
            "rounds": 0,
        }

    @staticmethod
    def _reject_all(d):
        d["rejected"] = list(
            dict.fromkeys(
                [
                    *d["rejected"],
                    *(item["id"] for item in d["displayed"]),
                ]
            )
        )

    @staticmethod
    def _feedback_reason(text):
        if re.search(r"太貴|便宜", text):
            return "price"
        # Taste/size were already resolved by meal_wishes, including negative feedback.
        # A size rejection permits trying a different size of the same dish.
        wishes, _ = meal_wishes(text)
        if "portion" in wishes:
            return "portion"
        return "another"

    def _reject_with_reason(self, d, reason, wishes=None):
        if reason not in {"another", "price", "type", "recent", "light", "portion"}:
            raise ValueError("無法辨識替換原因")
        if not d["displayed"]:
            raise ValueError("請先取得候選餐點")
        d["rounds"] = d.get("rounds", 0) + 1
        d["feedback"] = [*d["feedback"], {"scope": "all", "reason": reason}][-100:]
        notice = "換一批看看，保留你的預算與需求。"
        if reason == "price":
            prices = [i["total"] for i in d["displayed"] if i.get("total") is not None]
            if prices:
                d["prefs"]["cheaperThan"] = min(prices)
            notice = "這批太貴了，改找比剛才更便宜的餐點。"
        elif reason == "light":
            d["prefs"]["taste"] = "light"
            notice = "改往清爽、少油的方向找，預算與忌口保留。"
        elif reason == "portion":
            d["prefs"]["portion"] = "large"
            d["exactRejections"] = [*d.get("exactRejections", []),
                                    *(i["id"] for i in d["displayed"])]
            notice = "優先找菜單標示大份的餐點，預算與忌口保留。"
        elif reason == "type":
            kinds = {i["dishType"] for i in d["displayed"] if i.get("dishType")}
            d["prefs"]["rejectedTypes"] = sorted(
                kinds | set(d["prefs"].get("rejectedTypes", []))
            )
            d["prefs"].pop("dishType", None)
            notice = "這餐先避開剛才的餐點類型，其他條件保留。"
        self._reject_all(d)
        d["prefs"].update(wishes or {})
        return notice

    def _choose(self, state, session_id, d, item_id, text):
        selected = self._displayed(d, item_id)
        d["displayed"] = [selected]
        d["phase"] = "selection"
        return self._save(
            state,
            session_id,
            d,
            self._view(d, "selection", "這餐就吃這個。", items=[selected]),
            text or f"就吃這個：{selected['name']}",
        )

    def _save(self, state, session_id, d, view, user_text):
        d["interactions"] = d.get("interactions", 0) + 1
        d["revision"] = uuid.uuid4().hex
        view["revision"] = d["revision"]
        d["view"] = view
        state.decision = d
        for key in PERSISTENT_FIELDS:
            if d.get("persistentPrefs", {}).get(key):
                state.prefs[key] = copy.deepcopy(d["persistentPrefs"][key])
            else:
                state.prefs.pop(key, None)
        state.history.extend(
            [
                {"role": "user", "content": user_text, "meta": {"mode": "discovery"}},
                {"role": "assistant", "content": view["message"], "meta": {"decision": view}},
            ]
        )
        self.sessions.save(session_id)
        emit(
            "decision.step", mealId=d.get("mealId"), phase=d["phase"],
            questionCount=len(d["asked"]), interactions=d["interactions"],
            replacementRounds=d.get("rounds", 0),
            elapsedSeconds=round(time.time() - d.get("startedAt", time.time()), 2),
        )
        return copy.deepcopy(view)

    @staticmethod
    def _view(d, kind, message, **kwargs):
        return {
            "type": kind,
            "message": message,
            "restaurant": d["restaurant"],
            "constraints": _constraints(d["prefs"]),
            "questionCount": len(d["asked"]),
            "maxQuestions": MAX_QUESTIONS,
            "focused": bool(d.get("focused")),
            "dataNote": d.get("dataNote", ""),
            **kwargs,
        }

    @staticmethod
    def _displayed(d, item_id):
        if d["phase"] not in {"recommendation", "selection"}:
            raise ValueError("請先取得候選餐點")
        for item in d["displayed"]:
            if item["id"] == item_id:
                return item
        raise ValueError("這道餐點已不在目前候選中")

    @staticmethod
    def _answer(d, value):
        question = d.get("question")
        if not question or value not in {o["value"] for o in question["options"]}:
            raise ValueError("請使用目前問題的選項")
        if value != "skip":
            if question["field"] == "comparison":
                d["shortlistId"] = value
                d["focused"] = True
            else:
                d["prefs"][question["field"]] = (
                    float(value) if question["field"] == "budget" else value
                )
        d["question"] = None

    @staticmethod
    def _next_question(d, rows, *, allow_comparison=False):
        if d.get("question"):
            return d["question"]
        prefs = d["prefs"]
        questions = []
        answered = set(d["asked"]) | set(d.get("skipped", []))
        prices = sorted({row["total"] for row in rows if row["total"] is not None})
        if (
            "budget" not in prefs
            and "budget" not in answered
            and len(prices) >= 3
            and prices[-1] - prices[0] >= 20
        ):
            bounds = sorted({prices[len(prices) // 3], prices[(len(prices) * 2) // 3]})
            question = {
                "field": "budget",
                "title": "這一餐，每份主餐想花多少？",
                "options": [
                    *[_option(f"{p:g}", f"$ {p:g} 以內") for p in bounds],
                    _option("skip", "先不設上限"),
                ],
            }
            remaining = sum(sum(r["total"] is not None and r["total"] <= p for r in rows)
                            for p in bounds) / len(bounds)
            questions.append((1 - remaining / len(rows), question))
        for field, title in (
            ("dishType", "現在比較想吃哪一類？"),
            ("texture", "這餐想吃湯的，還是乾的？"),
            ("protein", "從菜單品名來看，現在比較想選哪個方向？"),
            ("taste", "想吃清爽的，還是重口味的？"),
            ("portion", "這餐想吃大份一點，還是小份一點？"),
        ):
            if field in prefs or field in answered:
                continue
            counts = Counter(row[field] for row in rows if row.get(field))
            known = sum(counts.values())
            values = sorted(counts)
            if 2 <= len(values) <= 4 and known / len(rows) >= 0.7:
                labels = SOFT_LABELS.get(field, {})
                question = {
                    "field": field,
                    "title": title,
                    "options": [*[_option(v, labels.get(v, v)) for v in values],
                                _option("skip", "都可以")],
                }
                # Expected reduction, discounted when many dishes have no evidence.
                gain = (1 - sum(n * n for n in counts.values()) / known**2)
                questions.append((gain * known / len(rows), question))
        if questions:
            return max(questions, key=lambda entry: entry[0])[1]
        if allow_comparison and "comparison" not in d["asked"]:
            options = DecisionService._pick(rows, [])
            if len(options) > 1:
                return {
                    "field": "comparison",
                    "title": "先看看實際餐點，哪一道比較有意願？選了還可以再調整。",
                    "options": [*[_option(row["id"], row["name"]) for row in options],
                                _option("skip", "都可以，先看推薦")],
                }
        return None

    @staticmethod
    def _pick(rows, keep_ids):
        rows = sorted(
            rows,
            key=lambda r: (
                -r.get("softScore", 0),
                bool(r["warnings"]),
                -r["likes"],
                r["total"] is None,
                r["total"] or 0,
                r["name"],
            ),
        )
        picked = [r for item_id in keep_ids for r in rows if r["id"] == item_id][:3]
        # 三輪逐步放寬。同一道菜的修飾版（霜降牛小排／玫瑰霜降牛小排／霜降牛小排切厚切）
        # family 字串不相等，光靠 dishType 擋不掉，會推出三個看起來一樣的選項。
        for avoid_type, avoid_similar in ((True, True), (False, True), (False, False)):
            for row in rows:
                if len(picked) >= 3:
                    return picked
                if any(r["id"] == row["id"] or r["family"] == row["family"] for r in picked):
                    continue
                if avoid_type and any(r["dishType"] == row["dishType"] for r in picked):
                    continue
                if avoid_similar and any(
                    _related_family(r["family"], row["family"]) for r in picked
                ):
                    continue
                picked.append(row)
        return picked

    @staticmethod
    def _card(row, prefs, rate):
        reasons = list(row.get("evidence", []))
        if prefs.get("budget") is not None:
            reasons.append("菜單金額在這次預算內" if rate is None else "含服務費在預算內")
        if prefs.get("dishType"):
            reasons.append(f"符合這次想吃{prefs['dishType']}的需求")
        if prefs.get("texture"):
            reasons.append(f"這次想吃{prefs['texture']}")
        if prefs.get("protein"):
            reasons.append(f"品名符合你選的{prefs['protein']}方向")
        if row["likes"]:
            reasons.append("品名符合你提到的喜好")
        if not reasons:
            reasons.append(f"{row['dishType'] or row['category']}，給你一個選擇方向")
        return {
            key: row[key] for key in ("id", "name", "price", "total", "dishType", "warnings")
        } | {
            "reason": "；".join(reasons),
            "spice": copy.deepcopy(row["semantic"].get("spice", {})),
            "serviceRate": rate,
            "priceNote": (
                "菜單價格；服務費未提供，結帳金額需向店家確認"
                if rate is None
                else f"含 {rate * 100:g}% 服務費"
            ),
        }

    @staticmethod
    def _text_action(d, text):
        text = text.strip().strip("。！!？? ")
        q = d.get("question")
        if re.search(r"(?:幫我|替我)(?:決定|選一道|挑一道)|直接幫我選|不用問了", text):
            return "decide", None, None
        if text in {"直接推薦", "隨便"}:
            return "recommend", None, None
        if q:
            if text in {"都可以", "都行", "沒差", "跳過", "不知道", "不限", "沒有預算"}:
                return "answer", "skip", None
            for index, option in enumerate(q["options"]):
                if text in {option["label"], option["value"], f"第{'一二三四五六七八九'[index]}個"}:
                    return "answer", option["value"], None
        if re.search(r"都不想|都不要|換一批|換一道|換一個|換別的|其他的", text):
            return "reject_all", None, None
        if re.search(r"太貴|便宜一點|更便宜", text):
            return "cheaper", None, None
        if re.search(r"就吃|就選|我要第|選第", text):
            for index, row in enumerate(d.get("displayed", [])):
                if (
                    row["name"] in text
                    or f"第{'一二三'[index]}個" in text
                    or f"第{index + 1}個" in text
                ):
                    return "choose", None, row["id"]
            if len(d.get("displayed", [])) == 1:
                return "choose", None, d["displayed"][0]["id"]
        return "message", None, None

    @staticmethod
    def _apply_text(d, text):
        prefs = d["prefs"]
        before = copy.deepcopy(prefs)
        persistent_before = copy.deepcopy(d.get("persistentPrefs", {}))
        skipped_before = list(d.get("skipped", []))
        wishes, remaining = meal_wishes(text)
        remaining = re.sub(r"(?<!不)吃素(?!食)", "吃素食", remaining)
        delta = parse_preferences(remaining)
        delta.pop("weights", None)
        merge_preference_delta(prefs, delta)
        prefs.update(wishes)
        persistent = d.setdefault("persistentPrefs", {})
        temporary = bool(re.search(r"今天|這餐|這一餐|這次|暫時|先不|不想吃|剛吃", text))
        keys = {"allergens"} if temporary else set(PERSISTENT_FIELDS)
        fixed_delta = {key: value for key, value in delta.items() if key in keys}
        fixed_delta["_operations"] = [
            op for op in delta.get("_operations", [])
            if op.get("field") in keys or (op.get("field") == "dislikes" and not temporary)
            or (op.get("action") == "clear" and op.get("field") == "all")
        ]
        merge_preference_delta(persistent, fixed_delta)
        if prefs.get("people", 1) > 1 and prefs.get("budgetBasis") == "total" and "budget" in delta:
            prefs["budget"] /= prefs["people"]
        if re.search(r"預算不限|不限預算|不設上限|取消預算", text):
            prefs.pop("budget", None)
            prefs.pop("cheaperThan", None)
            d["skipped"] = list(dict.fromkeys([*d.get("skipped", []), "budget"]))
            if (d.get("question") or {}).get("field") == "budget":
                d["question"] = None
        if "budget" in delta:
            prefs.pop("cheaperThan", None)
        for pattern, field, value in (
            (r"^(?:飯|飯類)$|(?:想吃|要吃|改吃|改成|換成|換吃|我要)(?:飯|飯類)", "dishType", "飯"),
            (r"^(?:麵|麵類)$|(?:想吃|要吃|改吃|改成|換成|換吃|我要)(?:麵|麵類)", "dishType", "麵"),
            (r"想吃湯|湯的|喝湯", "texture", "湯的"),
            (r"乾的|乾麵|乾拌|不要湯|不想喝湯", "texture", "乾的"),
        ):
            if re.search(pattern, text) and not re.search(r"不(?:要|想|吃).*" + value, text):
                prefs[field] = value
                prefs["rejectedTypes"] = [v for v in prefs.get("rejectedTypes", []) if v != value]
        for value, aliases in (
            ("牛肉", "牛肉|牛"), ("雞肉", "雞肉|雞"), ("豬肉", "豬肉|豬"),
            ("羊肉", "羊肉|羊"), ("魚類", "魚類|魚"), ("蝦蟹", "蝦蟹|蝦|蟹"),
            ("蔬食", "蔬食|素食"),
        ):
            if re.search(rf"^(?:{aliases})$|(?:改吃|換吃|改成|換成)(?:{aliases})", text):
                if not re.search(rf"不(?:要|想|吃).*(?:{aliases})", text):
                    prefs["protein"] = value
        if re.search(r"肉類不限|取消肉類|肉類都可以", text):
            prefs.pop("protein", None)
        if re.search(r"飯麵都可以|類型不限|取消類型", text):
            prefs.pop("dishType", None)
            prefs.pop("texture", None)
            prefs.pop("rejectedTypes", None)
        if before != prefs and d.get("question"):
            field = d["question"]["field"]
            if field in prefs or field == "comparison":
                d["question"] = None
        return (before != prefs or persistent_before != persistent
                or skipped_before != d.get("skipped", []))
