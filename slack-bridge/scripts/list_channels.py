"""Bot が参加しているチャンネルの channel_id を一覧表示する。

config/projects.yaml のプレースホルダを埋めるための補助スクリプト。

使い方:
    python scripts/list_channels.py

事前に Bot を対象チャンネルへ招待しておくこと（Slack で `/invite @ClaudeCode`）。
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


def main() -> int:
    force_utf8_stdio()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        print("SLACK_BOT_TOKEN が未設定です（.env を確認してください）", file=sys.stderr)
        return 1

    client = WebClient(token=token)
    cursor = None
    rows: list[tuple[str, str]] = []

    try:
        while True:
            resp = client.users_conversations(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for channel in resp["channels"]:
                rows.append((channel["id"], channel["name"]))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as exc:
        print(f"Slack API エラー: {exc.response.get('error')}", file=sys.stderr)
        print(
            "必要なスコープ: channels:read, groups:read（Bot を対象チャンネルに招待済みか確認）",
            file=sys.stderr,
        )
        return 1

    if not rows:
        print("Bot が参加しているチャンネルがありません。`/invite @ClaudeCode` で招待してください。")
        return 0

    print(f"{'channel_id':<14} name")
    print("-" * 40)
    for channel_id, name in sorted(rows, key=lambda r: r[1]):
        print(f"{channel_id:<14} #{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
