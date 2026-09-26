"""イベントの重複と同時実行を抑える道具。

``slack_handler`` から切り出した。実行フロー側（``task_flow``）も ``ThreadLocks`` を
使うため、振り分け側に置いたままだと循環 import になる。
"""

from __future__ import annotations

import threading
from collections import OrderedDict


class SeenEvents:
    """処理済み event_id の有限リングバッファ。

    Slack はハンドラが数秒以内に ack しないとイベントを再送する。Claude Code の実行は
    分単位でかかるため再送は起こりうるし、ネットワークの切断・再接続でも起こる。
    冪等化しないと同一タスクが多重起動し、調査なら無駄な課金で済むが実装タスクでは
    同じ変更が二重に走る。
    """

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._items: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def add_if_new(self, event_id: str | None) -> bool:
        """未処理なら記録して True。既に処理済みなら False。"""
        if not event_id:
            return True
        with self._lock:
            if event_id in self._items:
                return False
            self._items[event_id] = None
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return True


class ThreadLocks:
    """Slack スレッドごとのロック。

    同じスレッドへ続けて依頼が飛んだ場合、Claude Code の同一セッションを2つの
    プロセスが同時に更新してしまう。スレッド単位で直列化してこれを防ぐ。
    別スレッド・別プロジェクト同士は当然並行に走ってよい。
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def for_thread(self, channel_id: str, thread_ts: str) -> threading.Lock:
        key = f"{channel_id}:{thread_ts}"
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock
