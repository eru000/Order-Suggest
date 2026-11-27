"""節點 6：LLM 說明的消融實驗（委員5-9「移除 LLM 會不會影響推薦」）。

推薦本身由規則引擎決定（USE_LLM_EXTRACTION 預設關閉，偏好解析也不經 LLM）；
LLM 只負責把推薦結果寫成自然語言，以及回答模板答不了的開放問題。這裡把兩件事分開量：

A. 推薦說明：同一份結構化推薦，分別產生「模板說明」與「LLM 說明」，自動事實檢查
   - itemCoverage   推薦的菜有幾道在文字裡被提到
   - unsupportedNumbers  文字中的金額（$N、N 元）不是任何推薦品項價格、小計、總額或預算
   - inventedDishes 文字提到菜單上有、但這次沒推薦的菜名
   - 延遲、失敗（LLM 失敗時產品會靜默改用模板，這裡直接呼叫 LLM 以如實記錄）
B. 開放問答：open_questions.json 的問題由 LLM 回答，輸出 CSV 給兩位評分者獨立判斷對錯。

用法
  Windows（打 API）：python -X utf8 evals/llm_reply/llm_ablation.py live
  任何環境（離線計分）：python -X utf8 evals/llm_reply/llm_ablation.py score <live 結果 json>
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals" / "recommendation"))

OUT = ROOT / "evals" / "results" / "step6"
# 金額與菜名的檢查與產品的輸出防護共用同一支（src/reply_guard.py），兩邊不會分岔。
from reply_guard import _cn_number, check_reply, fact_check, to_arabic  # noqa: E402,F401


def _cases() -> list[dict[str, Any]]:
    return json.loads((ROOT / "evals" / "recommendation" / "cases.json").read_text(encoding="utf-8"))["cases"]


def _menus() -> dict[str, dict[str, Any]]:
    from run_eval import load_menus

    return load_menus(ROOT)


def _recommendation(menu: dict[str, Any], text: str) -> dict[str, Any]:
    from main import prepare_recommendation

    return prepare_recommendation([], text, menu, {})


def _menu_names(menu: dict[str, Any]) -> list[str]:
    """菜單上的品名，排除與分類名稱相同的（「飲料」「季節時蔬」同時是分類標籤）。"""
    from recommendation import _flatten_menu

    items = _flatten_menu(menu)
    categories = {str(item.get("category") or "") for item in items}
    return sorted(
        {str(item["name"]) for item in items if str(item["name"]) not in categories},
        key=len, reverse=True,
    )


def live() -> int:
    from main import _build_recommendation_prompt, _fallback_format
    from ollama_fuc import chat

    menus = _menus()
    rows = []
    for case in _cases():
        menu = menus[case["restaurant"]]
        rec = _recommendation(menu, case["text"])
        messages = _build_recommendation_prompt(rec, case["text"], menu)
        started = time.perf_counter()
        try:
            llm_text, error = chat(messages, timeout=180.0), None
        except Exception as exc:  # 產品會靜默降級；實驗要記下來
            llm_text, error = "", f"{type(exc).__name__}: {exc}"[:300]
        rows.append({
            "kind": "explain", "restaurant": case["restaurant"], "text": case["text"],
            "recommendation": rec, "template": _fallback_format(rec),
            "llm": llm_text, "llmError": error, "llmSeconds": round(time.perf_counter() - started, 2),
        })
        print(f"  說明 {case['restaurant']}「{case['text']}」 {rows[-1]['llmSeconds']}s {error or ''}")
    questions = json.loads((HERE / "open_questions.json").read_text(encoding="utf-8"))["questions"]
    for question in questions:
        menu = menus[question["restaurant"]]
        rec = _recommendation(menu, question["question"])
        messages = _build_recommendation_prompt(rec, question["question"], menu)
        started = time.perf_counter()
        try:
            llm_text, error = chat(messages, timeout=180.0), None
        except Exception as exc:
            llm_text, error = "", f"{type(exc).__name__}: {exc}"[:300]
        rows.append({
            "kind": "open", **question, "template": _fallback_format(rec),
            "llm": llm_text, "llmError": error, "llmSeconds": round(time.perf_counter() - started, 2),
        })
        print(f"  問答 {question['id']} {rows[-1]['llmSeconds']}s {error or ''}")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"llm-live-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已存到 {path.relative_to(ROOT)}")
    return 0


def score(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    menus = _menus()
    explain = [row for row in data["rows"] if row["kind"] == "explain"]
    results = []
    for row in explain:
        names = _menu_names(menus[row["restaurant"]])
        results.append({
            "restaurant": row["restaurant"], "text": row["text"],
            "template": fact_check(row["template"], row["recommendation"], names),
            "llm": fact_check(row["llm"], row["recommendation"], names) if row["llm"] else None,
            # 產品的輸出防護（src/reply_guard.check_reply）對這則回覆的判定
            "guard": (
                {k: v for k, v in check_reply(
                    row["llm"], row["recommendation"], menus[row["restaurant"]]
                ).items() if k in ("ok", "reasons", "missingItems", "unsupportedNumbers")}
                if row["llm"] else None
            ),
            "llmError": row["llmError"], "llmSeconds": row["llmSeconds"],
        })

    def total(kind: str, key: str) -> int:
        return sum(len(r[kind][key]) if isinstance(r[kind][key], list) else r[kind][key]
                   for r in results if r[kind])

    ok = [r for r in results if r["llm"]]
    summary = {
        "cases": len(results),
        "llmFailures": sum(1 for r in results if not r["llm"]),
        "llmMedianSeconds": sorted(r["llmSeconds"] for r in ok)[len(ok) // 2] if ok else None,
        "template": {k: total("template", k) for k in
                     ("itemCount", "itemsMentioned", "unsupportedNumbers", "inventedDishes", "chars")},
        "llm": {k: total("llm", k) for k in
                ("itemCount", "itemsMentioned", "unsupportedNumbers", "inventedDishes", "chars")},
        "guardPassed": sum(1 for r in results if r["guard"] and r["guard"]["ok"]),
        "guardRejected": sum(1 for r in results if r["guard"] and not r["guard"]["ok"]),
    }
    open_rows = [row for row in data["rows"] if row["kind"] == "open"]
    csv_path = OUT / "open-questions-for-raters.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "restaurant", "question", "expected_hint", "llm_answer",
                         "template_answer", "rater1_correct(1/0)", "rater2_correct(1/0)", "note"])
        for row in open_rows:
            writer.writerow([row["id"], row["restaurant"], row["question"], row.get("expectedHint", ""),
                             row["llm"] or f"(失敗) {row['llmError']}", row["template"], "", "", ""])
    out_path = OUT / "llm-ablation-scored.json"
    out_path.write_text(json.dumps({"source": str(path.relative_to(ROOT)), "summary": summary,
                                    "cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in results:
        if r["llm"] and (r["llm"]["unsupportedNumbers"] or r["llm"]["inventedDishes"]):
            print(f"  LLM {r['restaurant']}「{r['text']}」 不符金額 {r['llm']['unsupportedNumbers']} "
                  f"未推薦卻提到 {r['llm']['inventedDishes'][:5]}")
    print(f"已存到 {out_path.relative_to(ROOT)}；開放問答評分表 {csv_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "live":
        raise SystemExit(live())
    if len(sys.argv) >= 3 and sys.argv[1] == "score":
        raise SystemExit(score(Path(sys.argv[2]).resolve()))
    print(__doc__)
    raise SystemExit(2)
