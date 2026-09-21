# Slack Bridge を確実に再起動する。設定やコードを変更したあとはこれを使う。
#
#     .\scripts\restart_bridge.ps1
#
# Stop-ScheduledTask → Start-ScheduledTask だけでは古い python が生き残り、
# 多重起動になる。stop_bridge.ps1 でプロセスツリーごと落としてから起動する。

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$TaskName = "SlackBridge"

& (Join-Path $Root "scripts\stop_bridge.ps1")

Write-Host "`n--- 起動 ---"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 6

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize

# インスタンス数はロックファイルで数える。
# venv の python.exe は本体を再実行するシムなので、コマンドラインで数えると
# 1インスタンスでも 2 プロセスに見えてしまう（親 python → 子 python）。
$lock = Join-Path $Root "state\bridge.pid"
if (Test-Path $lock) {
    $lockPid = (Get-Content $lock -Encoding UTF8).Trim()
    $alive = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
    if ($alive) {
        Write-Host "ブリッジは1インスタンスで動作しています (pid $lockPid)。"
    } else {
        Write-Warning "ロックファイルの pid $lockPid が生きていません。起動に失敗した可能性があります。"
    }
} else {
    Write-Warning "ロックファイルがありません。logs\bridge.log を確認してください。"
}

Write-Host "`n--- 起動ログ ---"
Get-Content (Join-Path $Root "logs\bridge.log") -Encoding UTF8 -Tail 6
