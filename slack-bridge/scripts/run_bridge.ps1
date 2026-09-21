# Slack Bridge の常駐用ラッパ。
#
# 落ちたら自動的に再起動する。タスクスケジューラからはこのスクリプトを起動する。
# 手元で動かして様子を見ることもできる:
#
#     .\scripts\run_bridge.ps1
#
# 終了コードの扱い:
#   0 … 意図的な停止（Ctrl+C）          → 再起動しない
#   2 … 設定・環境の不備                → 人手が必要なので再起動しない
#   その他 … クラッシュ                  → バックオフして再起動する

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
$SupervisorLog = Join-Path $LogDir "supervisor.log"

if (-not (Test-Path $Python)) {
    Write-Error "venv が見つかりません: $Python`nslack-bridge で python -m venv venv を実行してください。"
    exit 2
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Write-Supervisor([string]$Message) {
    $line = "{0} [supervisor] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $SupervisorLog -Value $line -Encoding utf8
}

Set-Location $Root

# 再起動の間隔。連続で落ちるほど長く待つ（Slack 側の障害を叩き続けないため）。
$MinDelay = 5
$MaxDelay = 300
$delay = $MinDelay

Write-Supervisor "監視を開始します (root=$Root)"

while ($true) {
    $startedAt = Get-Date
    Write-Supervisor "Slack Bridge を起動します"

    & $Python -m app.main
    $code = $LASTEXITCODE

    $ranFor = [int]((Get-Date) - $startedAt).TotalSeconds

    if ($code -eq 0) {
        Write-Supervisor "正常終了しました（稼働 ${ranFor}秒）。監視を終了します。"
        break
    }
    if ($code -eq 2) {
        Write-Supervisor "設定エラーで終了しました（exit=2）。logs\bridge.log を確認してください。監視を終了します。"
        break
    }

    # 十分に稼働できていたなら、一時的な障害とみなして待ち時間をリセットする
    if ($ranFor -ge 120) { $delay = $MinDelay }

    Write-Supervisor "異常終了しました（exit=$code / 稼働 ${ranFor}秒）。${delay}秒後に再起動します。"
    Start-Sleep -Seconds $delay

    $delay = [Math]::Min($delay * 2, $MaxDelay)
}
