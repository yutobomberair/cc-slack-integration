# Slack Bridge をタスクスケジューラへ登録し、ログオン時に自動起動させる。
#
#     .\scripts\install_task.ps1              # 登録して即時起動
#     .\scripts\install_task.ps1 -Uninstall   # 登録解除
#     .\scripts\install_task.ps1 -NoStart     # 登録のみ（起動しない）
#
# 実行ユーザーのまま動かす点が重要。SYSTEM やサービス用アカウントで動かすと
# USERPROFILE が変わり、Claude Code が %USERPROFILE%\.claude\.credentials.json を
# 見つけられず、すべてのタスクが認証エラーになる。

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$TaskName = "SlackBridge"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_bridge.ps1"

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "タスク '$TaskName' は登録されていません。"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "タスク '$TaskName' を登録解除しました。"
    exit 0
}

if (-not (Test-Path $Runner)) {
    Write-Error "run_bridge.ps1 が見つかりません: $Runner"
    exit 1
}

# -WindowStyle Hidden でコンソールを出さずに常駐させる。
# 様子を見たいときは logs\bridge.log を追尾する。
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

# ログオン中のユーザーとして動かす（対話セッション）。パスワード保存は不要。
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Slack から自宅PCの Claude Code へ開発タスクを渡すブリッジ" `
    -Force | Out-Null

Write-Host "タスク '$TaskName' を登録しました。"
Write-Host "  実行ユーザー : $env:USERDOMAIN\$env:USERNAME"
Write-Host "  トリガー     : ログオン時"
Write-Host "  スクリプト   : $Runner"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "`nタスクを起動しました。状態を確認します…"
    Start-Sleep -Seconds 3
    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName, State |
        Format-Table -AutoSize
    Write-Host "ログの追尾:"
    Write-Host "  Get-Content -Wait -Tail 30 `"$(Join-Path $Root 'logs\bridge.log')`""
}
