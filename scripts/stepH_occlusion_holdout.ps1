# 節點 H 封存組遮蔽實驗：35 張遮蔽圖 × 凍結設定（預設提示詞）＝約 35 次學校 API 呼叫，約 10～15 分鐘
# 凍結（tag freeze-final）後才產生的衍生圖，產生器與種子（20261127）與開發組相同。每張只跑一次。
# 執行：powershell -ExecutionPolicy Bypass -File scripts\stepH_occlusion_holdout.ps1
# 結果寫入 evals\results\holdout\occlusion-windows-run.txt；分析由 Claude 在 Linux 環境做。
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Split-Path -Parent $PSScriptRoot)
$out = "evals\results\holdout\occlusion-windows-run.txt"
New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
$py = ".\.venv\Scripts\python.exe"
$cases = "evals\occlusion\holdout"
Remove-Item Env:VISION_PRICE_TEXT -ErrorAction SilentlyContinue
& {
  "== 執行時間 $(Get-Date -Format s)，程式版本 $(git rev-parse --short HEAD) =="
  "== 預估呼叫數 =="
  & $py -X utf8 evals\menu_ocr\run_eval.py --cases-dir $cases --dry-run 2>&1 | Select-Object -Last 3
  "== 封存組遮蔽：預設提示詞（凍結設定）=="
  & $py -X utf8 evals\menu_ocr\run_eval.py --cases-dir $cases --debug --jobs 2 --label occl-holdout-B 2>&1 | Select-Object -Last 8
  "== 完成 $(Get-Date -Format s) =="
} *>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $out
Write-Host "`n完成，結果在 $out，請回覆 Claude 繼續分析"
