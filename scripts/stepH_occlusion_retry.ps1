# 節點 H：重跑封存組遮蔽實驗逾時（HTTP 524）的 4 張迷客夏圖，4 次學校 API 呼叫，凍結設定
# 技術失敗重跑與開發組慣例相同；其他 31 張不重跑。
# 執行：powershell -ExecutionPolicy Bypass -File scripts\stepH_occlusion_retry.ps1
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Split-Path -Parent $PSScriptRoot)
$out = "evals\results\holdout\occlusion-windows-retry.txt"
$py = ".\.venv\Scripts\python.exe"
Remove-Item Env:VISION_PRICE_TEXT -ErrorAction SilentlyContinue
$lines = & {
  "== 執行時間 $(Get-Date -Format s) =="
  & $py -X utf8 evals\menu_ocr\run_eval.py --cases-dir evals\occlusion\holdout --debug --jobs 1 --label occl-holdout-B-retry `
      --case cafe_milksha__blur_full --case cafe_milksha__blur_partial `
      --case cafe_milksha__glare_full --case cafe_milksha__mask_full 2>&1 | Select-Object -Last 6
  "== 完成 $(Get-Date -Format s) =="
} *>&1 | ForEach-Object { "$_" }
$lines | ForEach-Object { Write-Host $_ }
[System.IO.File]::WriteAllLines((Join-Path (Get-Location) $out), [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
Write-Host "`n完成，請回覆 Claude"
