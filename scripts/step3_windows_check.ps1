# 第 3 項 Windows 端檢查：專案 .venv 重跑測試，並以 VISION_PRICE_TEXT=1 實打一次 dev 評測（約 3 次 API 呼叫）
# 執行：powershell -ExecutionPolicy Bypass -File scripts\step3_windows_check.ps1
# 結果同時寫入 evals\results\step3\windows-check.txt，Claude 會從那裡讀。
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$out = "evals\results\step3\windows-check.txt"
New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
$py = ".\.venv\Scripts\python.exe"
& {
  "== 執行時間 $(Get-Date -Format s) =="
  "== Python =="; & $py --version
  "== 單元測試 =="; & $py -X utf8 -m unittest discover -s tests -p "test_*.py" -t tests 2>&1 | Select-Object -Last 6
  "== 推薦評測 =="; & $py -X utf8 evals\recommendation\run_eval.py 2>&1 | Select-Object -Last 2
  "== 3.3 dev 實測：VISION_PRICE_TEXT=1 =="
  $env:VISION_PRICE_TEXT = "1"
  & $py -X utf8 evals\menu_ocr\run_eval.py --case xianghong --case web_dalai_noodles_soups --case web_fhgh_sets --debug --verbose --label step3-price-text 2>&1
  Remove-Item Env:VISION_PRICE_TEXT
  "== 與第一項基準比較 =="
  & $py -X utf8 evals\menu_ocr\run_eval.py --compare evals\results\2026-11-26-step1\ocr-live.json step3-price-text 2>&1
} *>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $out
Write-Host "`n完成，結果在 $out"
