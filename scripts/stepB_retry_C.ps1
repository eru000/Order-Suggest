# 節點 B：重跑 C 組逾時（HTTP 524）的 4 張大來小館遮蔽圖，4 次學校 API 呼叫
# 執行：powershell -ExecutionPolicy Bypass -File scripts\stepB_retry_C.ps1
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Split-Path -Parent $PSScriptRoot)
$out = "evals\results\stepB\windows-retry-C.txt"
$py = ".\.venv\Scripts\python.exe"
& {
  "== 執行時間 $(Get-Date -Format s) =="
  $env:VISION_PRICE_TEXT = "1"
  & $py -X utf8 evals\menu_ocr\run_eval.py --cases-dir evals\occlusion\dev --debug --jobs 1 --label occl-dev-C-retry `
      --case web_dalai_noodles_soups__blur_full --case web_dalai_noodles_soups__glare_full `
      --case web_dalai_noodles_soups__glare_partial --case web_dalai_noodles_soups__mask_full 2>&1 | Select-Object -Last 6
  Remove-Item Env:VISION_PRICE_TEXT
  "== 完成 $(Get-Date -Format s) =="
} *>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $out
Write-Host "`n完成，請回覆 Claude"
