# 點餐助手 Order-Suggest

到餐廳、不知道要吃什麼 → **拍下菜單** → App 辨識菜色與價格 → 依你的預算、人數、
忌口與辣度推薦要點什麼。

---

## 第一次設定（約 3 分鐘）

```bash
git clone https://github.com/eru000/Order-Suggest.git
cd Order-Suggest

python -m venv .venv
.venv\Scripts\activate        # macOS/Linux 用 source .venv/bin/activate

pip install -r requirements.txt
```

接著建立 `.env`：

```bash
copy .env.example .env        # macOS/Linux 用 cp
```

打開 `.env`，把 `API_KEY=` 填上金鑰。餐廳評價搜尋不需要額外的搜尋 API
金鑰。**金鑰不要提交到 GitHub。**

### 啟動

```bash
python -m uvicorn back:app --app-dir src --host 127.0.0.1 --port 7890
```

終端機會顯示 `Uvicorn running on http://127.0.0.1:7890`，瀏覽器開
http://localhost:7890 或 http://127.0.0.1:7890 都可以。
手機要連的話看 [DEPLOY.md](DEPLOY.md)，**需要先加防火牆規則**，否則一定連不上。

### 餐廳評價

在評價區輸入任意餐廳名稱後按「自動搜尋」；也可以直接使用目前菜單的餐廳名稱。
系統會搜尋公開來源、確認同名分店，整理推薦分、
資料信心、優缺點與可信度風險；結果會快取成 `reviews_餐廳名稱.json`。
需要改對應分店時按「換分店」重新選擇。評價更新會需要數十秒，請等候按鈕恢復。

`REVIEW_SEARCH_MODE=auto` 會先用 Playwright 開啟 Google Maps 公開店家頁，讀取店名、
地址與頁面公開的星等，再使用免金鑰的愛食記、Bing RSS、Google News RSS，以及
Dcard、PTT、痞客邦、PopDaily、WalkerLand 等定向查詢補充食記。只有相關來源不足時，
才回退到 Google 瀏覽器搜尋與 Jina／DuckDuckGo／Bing HTML 搜尋。程式不會繞過登入牆、
驗證碼，也不會捏造 Google 頁面未公開的評論數。

`POST /api/restaurant-review/refresh` 另接受 `source_urls` 字串陣列，可加入使用者提供的
Dcard、部落格或評論平台公開網址。第一次使用自動瀏覽器搜尋前請執行
`python -m playwright install chromium`；無法安裝瀏覽器的環境可把
`REVIEW_SEARCH_MODE` 改成 `rss`。

### 跑測試

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests
```

---

## 怎麼確認它真的有在運作

`generate_ai_reply()` 會把所有例外吞掉並降級成本機模板，所以**金鑰錯誤時畫面上
不會有任何錯誤提示**，只是回覆變得很快。

| 症狀 | 意義 |
|---|---|
| 對話回覆等 10–30 秒 | 正常，LLM 真的有被呼叫 |
| 對話**秒回** | 金鑰失效或連線斷了，你看到的是模板文字 |

後端 log 出現這行就是降級了：

```
[generate_ai_reply] 錯誤: ...，降級使用模板
```

---

## 專案結構

| 路徑 | 作用 |
|---|---|
| [src/back.py](src/back.py) | FastAPI 端點、session、靜態檔 |
| [src/main.py](src/main.py) | 偏好解析、對話流程、回覆生成 |
| [src/ollama_fuc.py](src/ollama_fuc.py) | 推薦引擎、LLM/VLM 呼叫、串流 |
| [src/menu_vision.py](src/menu_vision.py) | 菜單照片辨識（分塊 + 交叉校對） |
| [web/web.html](web/web.html) | 前端全部（單檔，UTF-16 編碼） |
| [DEPLOY.md](DEPLOY.md) | 部署方式、實測延遲、demo 腳本 |
| `menu_*.json` | 預先建好的餐廳菜單，demo 用 |

---

## 幾個實測數字（[DEPLOY.md](DEPLOY.md) 有完整說明）

| 動作 | 耗時 |
|---|---|
| 菜單照片辨識 | 約 10 秒 |
| 偏好解析 | 1 毫秒 |
| 推薦排序 | 0.0 秒（純本機計算） |
| 推薦結果顯示在畫面上 | **0.08 秒** |
| AI 說明文字開始浮現 | 10–25 秒 |

對話走 SSE 串流：推薦品項與價格先送（不必等 LLM），AI 的說明再逐段補上。
瀏覽器不支援串流時會自動退回一般請求。
