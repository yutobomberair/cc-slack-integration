"""長時間タスクの進捗表示（CLAUDE.md §12）。

開始時に投稿したメッセージを ``chat.update`` で書き換え続ける。新しいメッセージを
足していくとスレッドが経過報告で埋まるため、1本を更新し続ける方式にしている。

``chat.update`` は Slack のレート制限で言えば余裕のある部類だが、無駄打ちを避けるため
更新間隔は既定 30 秒としている（ワーカー1本なら毎分2回）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ProgressReporter:
    """実行中、開始メッセージを定期的に書き換えるデーモンスレッド。

    Slack への更新が失敗しても本処理は止めない。進捗表示は付加価値であって、
    タスクの成否とは独立しているため。
    """

    def __init__(
        self,
        client,
        channel_id: str,
        message_ts: str | None,
        render: Callable[[float], str],
        interval: float = 30.0,
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        self._message_ts = message_ts
        self._render = render
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at

    def start(self) -> None:
        if self._message_ts is None:
            return  # 開始メッセージの投稿に失敗している場合は何もしない
        self._thread = threading.Thread(target=self._loop, daemon=True, name="progress")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._update(self._render(self.elapsed))

    def _update(self, text: str) -> None:
        if self._message_ts is None:
            return
        try:
            self._client.chat_update(
                channel=self._channel_id, ts=self._message_ts, text=text
            )
        except Exception:  # noqa: BLE001 - 進捗表示の失敗で本処理を止めない
            logger.debug("進捗メッセージの更新に失敗しました", exc_info=True)

    def finish(self, text: str) -> float:
        """更新を止め、最終状態へ書き換える。経過秒数を返す。"""
        elapsed = self.elapsed
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._update(text)
        return elapsed
