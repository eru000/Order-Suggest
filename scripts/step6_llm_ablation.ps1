# 節點 6 LLM 消融：10 個推薦情境的 LLM 說明＋20 題開放問答＝30 次學校 LLM 呼叫
# 執行：powershell -ExecutionPolicy Bypass -File scripts\step6_llm_ablation.ps1
# 結果存到 evals\results\step6\llm-live-*.json，計分由 Claude 離線完成
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = ".\.venv\Scripts\python.exe"
& {
  "== 執行時間 $(Get-Date -Format s) =="
  & $py -X utf8 evals\llm_reply\llm_ablation.py live 2>&1 | Where-Object { $_ -notmatch '^\[event\]' }
  "== 完成 $(Get-Date -Format s) =="
} *>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath "evals\results\step6\windows-llm.txt"
Write-Host "`n完成，請回覆 Claude"
