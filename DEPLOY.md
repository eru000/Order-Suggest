# 部署與 Demo 指南

使用場景：在餐廳現場，拍下菜單 → App 辨識 → 推薦要點什麼。
所以 demo 一定要能在**手機**上跑，不能只有筆電。

下面三個方案的數據都是實際量測出來的，不是估計值。

---

## 先看這個：三個方案怎麼選

| | 網址 | 冷啟動 | 需要筆電開著 | 適合 |
|---|---|---|---|---|
| **A. 筆電 + 區網** | `http://192.168.x.x:7890` | 無 | 是 | **現場 demo 首選** |
| **B. Cloudflare Tunnel** | 公開 https 網址 | 無 | 是 | 手機用 4G、傳連結給老師 |
| **C. Render** | 永久 https 網址 | **約 50 秒** | 否 | 交作業、長期上線 |

**建議：demo 用 A，另外把 C 架好當備案。** C 的冷啟動 50 秒在台上會很難看，只適合當「這是我們的線上版」的佐證。

---

## 方案 A：筆電 + 區網（demo 首選）

### 1. 開防火牆（只要做一次，**必做**）

Windows 防火牆預設擋掉所有外部連入。不開這個，手機一定連不上，
而且症狀是「一直轉圈最後逾時」，很難第一時間看出是防火牆。

**以系統管理員身分**開 PowerShell：

```powershell
New-NetFirewallRule -DisplayName "Order-Suggest 7890" -Direction Inbound `
  -LocalPort 7890 -Protocol TCP -Action Allow -Profile Private
```

只開 `Private`（家用/信任網路）。學校 Wi-Fi 若被歸類為 `Public`，把 `-Profile Private`
改成 `-Profile Private,Public`——但那代表同一個網路的人都連得到，demo 完記得移除：

```powershell
Remove-NetFirewallRule -DisplayName "Order-Suggest 7890"
```

### 2. 啟動

```powershell
python -m uvicorn back:app --app-dir src --host 0.0.0.0 --port 7890
```

`--host 0.0.0.0` 是關鍵。用預設的 `127.0.0.1` 只綁本機，手機連不到。
啟動訊息會直接印出手機該用的網址。

### 3. 手機

連**同一個 Wi-Fi**，開 `http://<筆電IP>:7890`。查 IP：

```powershell
(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
  $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL' -and $_.IPAddress -notmatch '^169\.'
}).IPAddress
```

### 連不上的排查順序

1. 手機和筆電是不是同一個 Wi-Fi（**手機關掉行動網路**再試一次）
2. 防火牆規則加了沒
3. 學校 Wi-Fi 可能開了 AP isolation（裝置之間互相隔離）——這種**無解，改用方案 B**
4. 筆電睡眠會斷線，demo 前把電源設定改成不睡眠

---

## 方案 B：Cloudflare Tunnel（公開網址，不用同一個 Wi-Fi）

繞過所有區網與防火牆問題，手機用 4G 也連得到。免費、不用註冊。

```powershell
winget install --id Cloudflare.cloudflared
# 先照方案 A 啟動 uvicorn，然後另開一個視窗：
cloudflared tunnel --url http://localhost:7890
```

它會印出一個 `https://xxx-xxx.trycloudflare.com` 網址，手機直接開。

注意：**每次重啟網址都會變**，demo 前才開，開了就別關。
另外這個網址是公開的，任何人拿到都能用你的 API 金鑰額度，demo 完就把 tunnel 關掉。

---

## 方案 C：Render（永久網址）

repo 裡的 [render.yaml](render.yaml) 已經設定好，不用再寫任何東西。

1. Render 上選 **New + → Blueprint**，指向這個 repo
2. 它會問 `API_KEY`（其他變數 render.yaml 裡已經填好），貼上金鑰
3. 等 build 完成

### 免費方案的兩個限制

- **15 分鐘沒人用就休眠**，下一個請求要等約 50 秒喚醒。
  → demo 前 2 分鐘先用手機開一次把它叫醒。
- 檔案系統是暫存的。重新部署後，執行中新增的菜單會消失，
  但 repo 裡的 `menu_*.json` 一直都在，demo 用的餐廳不受影響。

金鑰 90 天到期時，改 Dashboard → Environment 的 `API_KEY`，不用重新部署整包。

---

## 環境變數

本機讀 `.env`（已在 `.gitignore`，不會進版控）。雲端則由平台的環境變數提供——
程式碼是「環境變數優先，沒有才讀 `.env`」，所以兩邊不會打架。

| 變數 | 值 | 說明 |
|---|---|---|
| `API_BASE_URL` | `https://api.ithu.tw/v1` | |
| `API_KEY` | *（機密）* | 90 天到期，過期要重發 |
| `API_MODEL` | `vibe` | 對話與推薦 |
| `VISION_MODEL` | `llama4scout` | 菜單照片辨識 |
| `VISION_VERIFY_MODEL` | `mistral-small-4` | 辨識結果覆核 |
| `HOST` | `0.0.0.0` | 本機才需要；雲端由平台決定 |
| `PORT` | `7890` | 雲端會自己塞 `$PORT` |
| `USE_LLM_EXTRACTION` | `false` | 開了會用 LLM 理解偏好，多約 1.5 秒 |

---

## 實測延遲（demo 前務必知道）

| 動作 | 實測 | 備註 |
|---|---|---|
| 菜單照片辨識 | 約 10 秒 | 6 品項全中 |
| 偏好解析 | **1 毫秒** | 純關鍵字比對 |
| 推薦排序 `recommend()` | **0.0 秒** | 本機計算，不打網路 |
| 產生回覆文字 | **11–28 秒** | 全部時間都在這裡 |
| Render 冷啟動 | 約 50 秒 | 只有免費方案有 |

拆解過了：**對話的耗時 100% 來自 `generate_ai_reply()` 那一次 LLM 呼叫**，
跟菜單大小幾乎無關（3 品項和 85 品項的餐廳耗時差不多）。11–28 秒的落差是
`api.ithu.tw` 本身的回應速度浮動，不是我們這邊的問題。

緩解方式：

- demo 前先跑一次把連線暖起來
- loading 動畫要明確顯示「AI 思考中」，不要看起來像當掉
- 真的要解，得改成串流輸出（邊生成邊顯示），而不是想辦法縮短總時間

### ⚠️ 兩個會讓延遲暴增／消失的陷阱

**1. `USE_LLM_CLASSIFICATION` 預設是 `true`，而且逐品項呼叫 LLM。**

程式碼裡的預設值是開啟（[src/ollama_fuc.py:491](src/ollama_fuc.py#L491)），
而 `classify_item()` 是**每個菜單品項各呼叫一次**。85 品項的菜單就是最多
85 次 LLM 呼叫，實測會讓單次對話衝到 57 秒。

本機 `.env` 有設 `false`，`render.yaml` 也有設 —— 但只要在**沒設這個變數的環境**
跑（例如自己手動 `uvicorn` 又沒載入 `.env`），就會踩到。部署到任何新平台時，
這個變數一定要顯式設成 `false`。

**2. 回覆「變很快」通常代表 LLM 根本沒被呼叫。**

`generate_ai_reply()` 把**所有例外都吞掉**，然後降級成本機模板
（[src/main.py:786-788](src/main.py#L786-L788)）。實測金鑰無效時回覆只要
**0.08 秒**，而且畫面上不會有任何錯誤提示。

所以：**如果 demo 當天回覆突然秒回，那不是變快了，是金鑰失效或連線斷了**，
你看到的是模板拼出來的文字（有 emoji、粗體、條列，語氣比較硬），不是 AI 寫的。
金鑰 90 天到期時就會是這個症狀。判斷方法是看後端 log 有沒有：

```
[generate_ai_reply] 錯誤: ...，降級使用模板
```

---

## Demo 前的檢查清單

- [ ] 防火牆規則加好，**用真的手機**連過一次（不是只用筆電瀏覽器測）
- [ ] 手機關掉行動網路，確認走 Wi-Fi 也能連
- [ ] 先拍一張真實菜單跑過辨識，確認會過
- [ ] 準備 2–3 張已經辨識成功的菜單照片當備案（現場網路掛掉時直接用預存的）
- [ ] 筆電電源設定改成不休眠
- [ ] 確認金鑰還沒到期（`GET /health` 通不代表金鑰有效，要實際發一次對話；
      **秒回 = 金鑰失效**，正常應該要等 10–30 秒）
- [ ] 確認 `USE_LLM_CLASSIFICATION=false` 有生效，否則對話會慢到快一分鐘
- [ ] 備案的備案：Cloudflare Tunnel 先裝好，區網掛了 30 秒內能切換
