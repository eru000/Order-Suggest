# OrderSuggest

繁體中文點餐助理：使用者上傳菜單照片 → VLM 辨識成結構化菜單 → 規則式推薦引擎
挑品項 → LLM 把結果轉成自然語言回覆。FastAPI + 原生 JS 前端，模型走學校的
OpenAI 相容 API（api.ithu.tw）。

## 驗證要求

**不要在沒有實際執行的情況下斷言原因。**

這個專案有過代價很高的教訓。一次辨識準確度調查裡，三個「讀完 code 很有把握」
的假設——`max_tokens` 截斷、2×2 切塊把直式菜單切壞、OCR 模型繁中太弱——
**全部是錯的**。真正的原因是 `normalize_vision_result` 對非 dict 的分類靜默
`continue`：模型把整個右半邊菜單讀得好好的，是解析這一關把它扔了。三個假設
不管動手改哪一個都不會有用，而且會讓人更確信「模型就是爛」。

所以：

1. **改完一定要跑測試。** 130 個測試 3 秒跑完，沒有不跑的理由。
2. **說「原因是 X」之前先證明 X。** 加 log、寫最小重現、或跑 evals。
3. **報告時分清楚哪些真的跑過、哪些是推測。** 是推測就明講是推測。
4. **測試綠燈不等於功能正確**——見下一節。

## 測試涵蓋不到的東西

`tests/` 把所有 vision 與 LLM 呼叫都 mock 掉了。測試全過**不代表**：

- 菜單辨識準不準
- AI 回覆的文字品質好不好

這兩件事只能實跑。辨識準確度用 `evals/menu_ocr/`（會真的打 API 燒配額，
刻意不進 CI）：

```powershell
.venv\Scripts\python.exe evals\menu_ocr\run_eval.py --dry-run          # 先看要燒幾次呼叫
.venv\Scripts\python.exe evals\menu_ocr\run_eval.py --label 這次改了什麼
.venv\Scripts\python.exe evals\menu_ocr\run_eval.py --compare 改動前 改動後
.venv\Scripts\python.exe evals\menu_ocr\run_eval.py --debug            # 記錄每次呼叫的原始往返
.venv\Scripts\python.exe evals\menu_ocr\run_eval.py --rescore 某次label # 改計分規則後免費重算
```

`--debug` 是分辨「切塊根本沒讀到」與「切塊讀到了但最終校對刪掉」的唯一方法。
兩者最終結果一樣，修法完全相反。

## 怎麼跑

Windows + PowerShell。Python 一律用專案的 venv，不要用系統的。

```powershell
# 測試：用 unittest，沒有裝 pytest。-t 必須給 tests，給 . 會 ImportError
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -t tests

# 啟動
.venv\Scripts\python.exe -m uvicorn back:app --app-dir src --reload
```

CI 另外會跑 ruff 與 mypy，但**範圍只有指定的那幾個檔案，不是全專案**，而且
這兩個工具預設沒裝在 venv 裡（只在 `requirements-dev.txt`）。要在本機跑先：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m ruff check src/recommendation.py src/security.py src/session_store.py src/persistence.py src/menu_library.py
.venv\Scripts\python.exe -m mypy src/recommendation.py src/security.py src/session_store.py src/persistence.py
```

中文輸出在 cp950 主控台會噴 `UnicodeEncodeError`，自己寫的 script 開頭要
`sys.stdout.reconfigure(encoding="utf-8")`。

## 幾個會誤導判斷的事實

- **學校 API 有伺服器端快取。** 實測：完全相同的請求第二次 0.25 秒回、內容
  一字不差；只差一個空白字元就變 1.2 秒且結果不同。所以重跑同一個設定
  **不是**獨立取樣，不能拿來估變異數。改 model / prompt / 圖片才會真的重算。
- **`.env` 在 `import ollama_fuc` 當下就被讀進來**（`_load_env_file()`，不是
  python-dotenv）。測試裡動 `os.environ` 要注意這個時序。
- **`USE_LLM_CLASSIFICATION` 必須維持 false。** 程式碼裡的預設值是 true，而它
  會對每個菜單品項各打一次 LLM——85 項的菜單會讓單次對話慢到 57 秒。
- **爬蟲與評論搜尋用不同機制**：`crawl_menu.py` 走 CDP 接本機 Chrome；
  `restaurant_reviews.py` 用 `chromium.launch()`，需要先
  `playwright install chromium`，沒裝會靜默退回 RSS 而不是報錯。
