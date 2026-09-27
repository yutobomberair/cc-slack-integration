"""通知先（仕様書 §12）。

    AlertManager
         ├── TerminalNotifier
         ├── FileNotifier
         ├── WindowsNotifier
         └── SlackNotifier

## statusline から直接送らない

通知の起点は statusline だが、**送信はそこで待たない**。Slack への HTTP や
PowerShell の起動に1〜3秒かかることがあり、数百ミリ秒ごとに呼ばれる statusline を
そこで止めると画面が固まる。

そこで statusline は別プロセスを切り離して起動し、すぐ戻る。

    python -m claude_usage.notifiers <JSON>

鳴る頻度は低い（枠ごとにレベルが上がったときだけ）ので、起動のコストは問題にならない。

## 認証情報は保存しない（仕様書 §17）

Slack のトークンは設定ファイルに書けるが、既定では環境変数 ``SLACK_BOT_TOKEN`` を
見る。通知の本文にもトークンは入れない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from claude_usage.alerts import Alert
from claude_usage.config import Config, Notifiers
from claude_usage.config import load as load_config
from claude_usage.snapshot import data_dir

#: レベルごとの見出し。Slack では絵文字に、それ以外では文字で出す。
_HEADERS = {
    "critical": ("CRITICAL", ":rotating_light:"),
    "high": ("HIGH", ":warning:"),
    "warning": ("WARNING", ":warning:"),
    "underuse": ("NOTICE", ":information_source:"),
}

_SLACK_URL = "https://slack.com/api/chat.postMessage"
_HTTP_TIMEOUT = 5


def log_path():
    return data_dir() / "alerts.log"


def _plain(alert: Alert) -> str:
    header = _HEADERS.get(alert.level, ("NOTICE", ""))[0]
    text = f"[{header}] {alert.headline}"
    if alert.detail:
        text += f"\n  {alert.detail}"
    return text


# --------------------------------------------------------------------------
# それぞれの通知先
# --------------------------------------------------------------------------

def notify_terminal(alert: Alert) -> bool:
    """標準エラーへ出す。statusline の1行を汚さないため stdout は使わない。"""
    try:
        sys.stderr.write(_plain(alert) + "\n")
        return True
    except (OSError, UnicodeEncodeError):
        return False


def notify_file(alert: Alert) -> bool:
    """追記で残す。

    いつ何が鳴ったかを後から確かめられないと、閾値が妥当か判断できない。
    既定で有効にしている理由がこれ。
    """
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = f"{stamp}\t{alert.level}\t{alert.window}\t{alert.headline}\t{alert.detail}"
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except OSError:
        return False


def notify_windows(alert: Alert) -> bool:
    """Windows のバルーン通知。

    外部モジュール（BurntToast 等）に頼らず、``System.Windows.Forms.NotifyIcon`` で
    出す。Windows 11 で追加インストールなしに動く。
    """
    header = _HEADERS.get(alert.level, ("NOTICE", ""))[0]
    title = f"Claude Code Usage — {header}"
    body = alert.headline + (f"\n{alert.detail}" if alert.detail else "")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        f"$n.BalloonTipTitle = {_ps_quote(title)};"
        f"$n.BalloonTipText = {_ps_quote(body)};"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(10000);"
        "Start-Sleep -Seconds 10;"
        "$n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=20,
            check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _ps_quote(text: str) -> str:
    """PowerShell の単一引用符文字列にする。中の ' は '' で escape する。"""
    return "'" + text.replace("'", "''") + "'"


def notify_slack(alert: Alert, settings: Notifiers) -> bool:
    """Slack へ投稿する。

    トークンは設定ファイルか環境変数から取る。**本文にトークンを含めない。**
    """
    token = settings.slack_token or os.environ.get("SLACK_BOT_TOKEN", "")
    channel = settings.slack_channel
    if not token or not channel:
        return False

    emoji = _HEADERS.get(alert.level, ("NOTICE", ""))[1]
    text = f"{emoji} *{alert.label}*\n{alert.headline}"
    if alert.detail:
        text += f"\n{alert.detail}"

    payload = json.dumps({"channel": channel, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        _SLACK_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
        return bool(body.get("ok"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


# --------------------------------------------------------------------------
# まとめて送る
# --------------------------------------------------------------------------

def deliver(alerts: list[Alert], config: Config | None = None) -> dict[str, int]:
    """有効な通知先すべてへ送る。返り値は通知先ごとの成功数。

    1つの通知先が落ちても他は送る。Slack が死んでいてもファイルには残したい。
    """
    settings = (config or load_config()).notifiers
    sent = {"terminal": 0, "file": 0, "windows": 0, "slack": 0}
    for alert in alerts:
        if settings.file and notify_file(alert):
            sent["file"] += 1
        if settings.terminal and notify_terminal(alert):
            sent["terminal"] += 1
        if settings.windows and notify_windows(alert):
            sent["windows"] += 1
        if settings.slack and notify_slack(alert, settings):
            sent["slack"] += 1
    return sent


def spawn(alerts: list[Alert]) -> bool:
    """別プロセスで送らせて、すぐ戻る。

    statusline から呼ぶための入口。Slack への HTTP や PowerShell の起動を待つと
    画面が固まるので、投げたら忘れる。
    """
    if not alerts:
        return False
    payload = json.dumps([vars(a) for a in alerts], ensure_ascii=False)
    try:
        kwargs = {}
        if os.name == "nt":
            # コンソールを出さず、親（statusline）から切り離す
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        subprocess.Popen(
            [sys.executable, "-m", "claude_usage.notifiers", payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        return True
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 1
    try:
        body = json.loads(args[0])
    except json.JSONDecodeError:
        return 1
    alerts = [
        Alert(
            window=item.get("window", ""),
            level=item.get("level", "warning"),
            headline=item.get("headline", ""),
            detail=item.get("detail", ""),
        )
        for item in body
        if isinstance(item, dict)
    ]
    deliver(alerts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
