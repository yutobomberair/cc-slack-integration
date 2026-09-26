"""スレッドへの返信をまとめる。

``chat_postMessage(channel=..., thread_ts=..., text=...)`` という同じ形が
14箇所に散っていた。宛先（チャンネルとスレッド）は1回の依頼の中では変わらないので、
先に束ねてしまえば呼び出し側は本文だけを気にすればよい。

投稿の失敗はここで飲む。返信できないこと自体は困るが、そのために本処理を
巻き戻す意味は無く、原因はログに残る（CLAUDE.md §14）。
"""

from __future__ import annotations

import logging

from app import formatting

logger = logging.getLogger(__name__)


class Thread:
    """1つの Slack スレッドへの返信口。"""

    def __init__(self, client, channel_id: str, thread_ts: str) -> None:
        # client は公開する。ファイルのアップロードのように、テキスト投稿以外の
        # API を叩く側が宛先とセットで受け取れるほうが取り回しがよい。
        self.client = client
        self.channel_id = channel_id
        self.thread_ts = thread_ts

    def post(self, text: str) -> str | None:
        """1件投稿し、その ts を返す（失敗したら None）。"""
        if not text:
            return None
        try:
            posted = self.client.chat_postMessage(
                channel=self.channel_id,
                thread_ts=self.thread_ts,
                text=text,
                mrkdwn=True,
            )
            return posted.get("ts")
        except Exception:  # noqa: BLE001 - 返信できなくても本処理は止めない
            logger.exception("Slack への投稿に失敗しました channel=%s", self.channel_id)
            return None

    def post_long(self, text: str) -> None:
        """Slack の1メッセージ上限を超える本文を分割して投稿する。

        途中で失敗したら打ち切る。続きを投げても同じ理由で失敗するため。
        """
        chunks = formatting.split_for_slack(text)
        if not chunks:
            chunks = ["(空の応答が返されました)"]
        for chunk in chunks:
            if self.post(chunk) is None:
                return
