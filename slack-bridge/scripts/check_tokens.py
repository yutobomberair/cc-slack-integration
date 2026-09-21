"""`.env` の2つのトークンが正しいかを、Slack へ接続する前に検証する。

    python scripts/check_tokens.py

xoxb- と xapp- は取り違えやすく、間違えたまま起動すると原因の分かりにくい接続
エラーになる。それぞれを実際に Slack API へ投げて、どちらが問題なのかを切り分ける。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.console import force_utf8_stdio  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from slack_sdk import WebClient  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

REQUIRED_BOT_SCOPES = {"app_mentions:read", "chat:write", "channels:read", "groups:read"}


def check_bot_token(token: str) -> bool:
    print("[SLACK_BOT_TOKEN]")
    if not token or token.endswith("実際の値") or token == "xoxb-...":
        print("  NG: 未記入です。OAuth & Permissions > Bot User OAuth Token を貼ってください")
        return False
    if not token.startswith("xoxb-"):
        prefix = token.split("-")[0]
        print(f"  NG: xoxb- で始まっていません（'{prefix}-' で始まっています）")
        if token.startswith("xapp-"):
            print("      → これは SLACK_APP_TOKEN の値です。2つが入れ替わっています")
        elif token.startswith("xoxp-"):
            print("      → User Token です。Bot User OAuth Token を使ってください")
        return False

    try:
        resp = WebClient(token=token).auth_test()
    except SlackApiError as exc:
        print(f"  NG: Slack に拒否されました（{exc.response.get('error')}）")
        return False

    print(f"  OK: team={resp['team']} bot={resp['user']}")

    granted = {s.strip() for s in (resp.headers.get("x-oauth-scopes") or "").split(",") if s.strip()}
    if granted:
        missing = REQUIRED_BOT_SCOPES - granted
        if missing:
            print(f"  警告: スコープ不足 {sorted(missing)}")
            print("        → OAuth & Permissions で追加し、再インストールしてください")
            return False
        print(f"  OK: 必要なスコープはすべて付与されています")
    return True


def check_app_token(token: str) -> bool:
    print("[SLACK_APP_TOKEN]")
    if not token or token.endswith("実際の値") or token == "xapp-...":
        print("  NG: 未記入です。Basic Information > App-Level Tokens の値を貼ってください")
        return False
    if not token.startswith("xapp-"):
        prefix = token.split("-")[0]
        print(f"  NG: xapp- で始まっていません（'{prefix}-' で始まっています）")
        if token.startswith("xoxb-"):
            print("      → これは SLACK_BOT_TOKEN の値です。2つが入れ替わっています")
        return False

    try:
        # Socket Mode の接続URLを実際に発行させる。ここが通れば起動時も接続できる。
        WebClient(token=token).apps_connections_open(app_token=token)
    except SlackApiError as exc:
        error = exc.response.get("error")
        print(f"  NG: Slack に拒否されました（{error}）")
        if error == "missing_scope":
            print("      → App-Level Token に connections:write スコープがありません")
        elif error == "not_allowed_token_type":
            print("      → Socket Mode が無効か、App-Level Token ではありません")
        return False

    print("  OK: Socket Mode の接続が確立できました")
    return True


def check_github_token(token: str) -> bool:
    """GitHub PAT は任意。未設定でも CI 通知をスキップするだけで他は動く。"""
    print("[GITHUB_TOKEN]（任意 / CI 通知に使用）")
    if not token:
        print("  未設定: GitHub Actions の結果通知は行われません（他の機能は動きます）")
        return True

    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "slack-bridge",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            who = json.loads(response.read().decode("utf-8")).get("login", "?")
    except urllib.error.HTTPError as exc:
        print(f"  NG: GitHub に拒否されました ({exc.code})")
        if exc.code == 401:
            print("      → トークンが無効か期限切れです")
        return False
    except urllib.error.URLError as exc:
        print(f"  NG: GitHub へ接続できませんでした: {exc.reason}")
        return False

    print(f"  OK: user={who}")
    print("  ※ プライベートリポジトリの run を読むには Actions の読み取り権限が必要です")
    return True


def main() -> int:
    force_utf8_stdio()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    ok_bot = check_bot_token(os.environ.get("SLACK_BOT_TOKEN", "").strip())
    print()
    ok_app = check_app_token(os.environ.get("SLACK_APP_TOKEN", "").strip())
    print()
    ok_gh = check_github_token(os.environ.get("GITHUB_TOKEN", "").strip())
    print()

    if ok_bot and ok_app and ok_gh:
        print("トークンは有効です。")
        return 0
    print("上記を修正してから再実行してください。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
