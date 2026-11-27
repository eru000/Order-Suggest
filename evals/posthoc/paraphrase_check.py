"""事後修正的換句話說檢查（開發者自編，非獨立資料）。

封存組找到的缺陷修完後，封存組本身已不能當獨立驗證。這裡在修正後、執行前先寫好
一批「同類但措辭不同」的句子與期望，只跑一次，看修正是不是只對準了封存組那幾句。
期望只看解析結果（排除詞、只要有標價、飯麵類別），不看推薦品項。

    python -X utf8 evals/posthoc/paraphrase_check.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from preference_engine import parse_preferences  # noqa: E402

DISH = re.compile(  # 與 decision_service._apply_text 相同的飯麵判斷
    r"^(?:飯|飯類)$|(?:想吃|要吃|改吃|改成|換成|換吃|我要|要)(?:飯類|飯)(?![店桶局])|飯類"
)

CASES = [
    # (句子, 期望排除詞, 期望只要有標價, 期望飯類)
    ("不要放洋蔥", ["洋蔥"], False, False),
    ("別放香菜", ["香菜"], False, False),
    ("幫我排除海鮮類", ["海鮮"], False, False),
    ("內臟類不要", ["內臟"], False, False),
    ("蔥這次先不要", ["蔥"], False, False),
    ("辣椒也不要了", ["辣椒"], False, False),
    ("不加蛋", ["蛋"], False, False),
    ("排除豬肉，其他照舊", ["豬肉"], False, False),
    ("再換一個，別重複剛才那道", [], False, False),
    ("同樣條件再換一道", [], False, False),
    ("今天先不要", [], False, False),
    ("沒標價的不要", [], True, False),
    ("只挑有標價的", [], True, False),
    ("價格要清楚", [], True, False),
    ("時價的就算了，只要確定有標價的", [], True, False),
    ("這道有標價嗎", [], False, False),
    ("晚餐想吃飯類", [], False, True),
    ("要飯不要麵", ["麵"], False, True),
    ("飯店附近有什麼", [], False, False),
    ("我要炒飯", [], False, True),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for text, excludes, known, rice in CASES:
        parsed = parse_preferences(text)
        got = {
            "excludes": parsed.get("excludes") or [],
            "requireKnownPrice": bool(parsed.get("requireKnownPrice")),
            "rice": bool(DISH.search(text)),
        }
        ok = got == {"excludes": excludes, "requireKnownPrice": known, "rice": rice}
        rows.append({"text": text, "expected": {"excludes": excludes, "requireKnownPrice": known,
                                                "rice": rice}, "got": got, "ok": ok})
        print(f"{'通過' if ok else '失敗'}  {text}  →  {got}")
    passed = sum(r["ok"] for r in rows)
    print(f"通過 {passed}/{len(rows)}")
    out = ROOT / "evals" / "results" / "posthoc" / "paraphrase-check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"passed": passed, "total": len(rows), "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
