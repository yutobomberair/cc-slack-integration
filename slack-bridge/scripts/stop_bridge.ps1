# Slack Bridge を確実に停止する。
#
#     .\scripts\stop_bridge.ps1
#
# Stop-ScheduledTask は監視ラッパ（powershell.exe）しか終了させず、その子である
# python プロセスが生き残る。生き残ったインスタンスは Slack への接続を保ったまま
# なので、次に起動すると複数インスタンスが並走し、
#
#   * イベントがどのインスタンスに届くか不定になる
#   * セッション記録が食い違い "Session ID ... is already in use" が出る
#   * Claude Code が二重に走り課金も倍になる
#
# という状態になる。実際にこれが起きたため、プロセスツリーごと落とす。

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$TaskName = "SlackBridge"

# 1. タスクを止める（監視ラッパを終了させ、再起動ループを断つ）
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        Write-Host "タスク '$TaskName' を停止しました。"
    } else {
        Write-Host "タスク '$TaskName' は動作していません。"
    }
} else {
    Write-Host "タスク '$TaskName' は登録されていません。"
}

Start-Sleep -Seconds 1

# 2. 取り残された python / powershell を落とす
$patterns = @(
    "*slack-bridge*app.main*",
    "*slack-bridge*run_bridge*"
)
$killed = 0
foreach ($name in @("python.exe", "powershell.exe")) {
    Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction SilentlyContinue |
        Where-Object {
            $cmd = $_.CommandLine
            $cmd -and ($patterns | Where-Object { $cmd -like $_ })
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host ("  停止: {0} (pid {1})" -f $name, $_.ProcessId)
            $killed++
        }
}
if ($killed -eq 0) { Write-Host "取り残されたプロセスはありませんでした。" }

# 3. ロックファイルを片付ける
$lock = Join-Path $Root "state\bridge.pid"
if (Test-Path $lock) {
    Remove-Item $lock -Force
    Write-Host "ロックファイルを削除しました。"
}

Start-Sleep -Seconds 1
Write-Host "`n=== 残存プロセス ==="
$remaining = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*slack-bridge*" }
if ($null -eq $remaining) {
    Write-Host "なし（完全に停止しました）"
} else {
    $remaining | Select-Object ProcessId, CommandLine | Format-Table -AutoSize
}
