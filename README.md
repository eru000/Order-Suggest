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

打開 `.env`，把 `API_KEY=` 填上金鑰。**金鑰不在 repo 裡**，跟組員要（私訊傳，
不要貼進 GitHub）。金鑰 90 天到期，過期要去 api.ithu.tw 重發。

### 啟動

```bash
python -m uvicorn back:app --app-dir src --host 0.0.0.0 --port 7890
```

瀏覽器開 http://localhost:7890 。
手機要連的話看 [DEPLOY.md](DEPLOY.md)，**需要先加防火牆規則**，否則一定連不上。

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
