# 節點 B 遮蔽實驗（開發資料）：25 張遮蔽圖 × 2 種提示詞＝50 次學校 API 呼叫，約 10～20 分鐘
#   B 組：預設提示詞（VISION_PRICE_TEXT=0）
#   C 組：照抄價格原文，看不清的位數用 ?（VISION_PRICE_TEXT=1）
# 執行：powershell -ExecutionPolicy Bypass -File scripts\stepB_occlusion_dev.ps1
# 結果寫入 evals\results\stepB\windows-run.txt；分析（OCR 交叉複核）由 Claude 在 Linux 環境做。
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Split-Path -Parent $PSScriptRoot)
$out = "evals\results\stepB\windows-run.txt"
New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
$py = ".\.venv\Scripts\python.exe"
$cases = "evals\occlusion\dev"
& {
  "== 執行時間 $(Get-Date -Format s) =="
  "== B 組：預設提示詞 =="
  $env:VISION_PRICE_TEXT = "0"
  & $py -X utf8 evals\menu_ocr\run_eval.py --cases-dir $cases --debug --jobs 2 --label occl-dev-B 2>&1 | Select-Object -Last 8
  "== C 組：VISION_PRICE_TEXT=1 =="
  $env:VISION_PRICE_TEXT = "1"
  & $py -X utf8 evals\menu_ocr\run_eval.py --cases-dir $cases --debug --jobs 2 --label occl-dev-C 2>&1 | Select-Object -Last 8
  Remove-Item Env:VISION_PRICE_TEXT
  "== 完成 $(Get-Date -Format s) =="
} *>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $out
Write-Host "`n完成，結果在 $out，請回覆 Claude 繼續分析"
