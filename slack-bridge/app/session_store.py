"""Slack スレッドと Claude Code セッションの対応を保持する（CLAUDE.md §11, §23.2）。

session_id は (channel_id, thread_ts) から UUIDv5 で決定的に導出する。したがって
「どの UUID を使うか」を保存する必要はなく、保存するのは「そのセッションを既に
作成済みかどうか」だけでよい。

保存しておく理由は、初回は ``--session-id`` で作成し、2回目以降は ``--resume`` で
継続する、という呼び分けがあるため。ブリッジを再起動しても継続できるよう、メモリ
ではなくファイルへ落とす。

なお記録が実態とずれた場合（セッションファイルが削除された等）は claude_runner 側で
新規セッションへフォールバックするので、この記録は「ヒント」であって正解ではない。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# UUIDv5 の名前空間。固定値であればよい。
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://slack-bridge.local/claude-session")


def derive_session_id(channel_id: str, thread_ts: str) -> str:
    """同じスレッドからは常に同じ session_id を導出する。"""
    return str(uuid.uuid5(_NAMESPACE, f"{channel_id}:{thread_ts}"))


class SessionStore:
    def __init__(self, path: Path, max_entries: int = 500) -> None:
        self._path = path
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._mtime_ns = 0
        self._data: dict[str, dict] = self._load()
        self._remember_mtime()

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("セッション記録を読めませんでした。空から開始します: %s", self._path)
            return {}

    def _remember_mtime(self) -> None:
        try:
            self._mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            self._mtime_ns = 0

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
            self._remember_mtime()
        except OSError:
            logger.exception("セッション記録の保存に失敗しました: %s", self._path)

    @staticmethod
    def _key(channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def _reload_if_changed(self) -> None:
        """ファイルが外部で更新されていれば読み直す。

        本来はブリッジが1インスタンスである前提だが、多重起動してしまった場合に
        メモリ上の記録が古いままだと「継続すべきスレッドを新規セッション扱い」して
        `Session ID ... is already in use` を招く。実際にそれが起きたので、
        安いチェック（mtime 比較）で保険をかける。
        """
        try:
            mtime = self._path.stat().st_mtime_ns
        except OSError:
            return
        if mtime != self._mtime_ns:
            self._data = self._load()
            self._mtime_ns = mtime

    def is_started(self, channel_id: str, thread_ts: str) -> bool:
        """このスレッドで既に Claude Code セッションを開始済みか。"""
        with self._lock:
            self._reload_if_changed()
            return self._key(channel_id, thread_ts) in self._data

    def record(self, channel_id: str, thread_ts: str, session_id: str, project_key: str) -> None:
        with self._lock:
            self._reload_if_changed()
            self._data[self._key(channel_id, thread_ts)] = {
                "session_id": session_id,
                "project": project_key,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            # 古いものから間引く（更新時刻順）
            if len(self._data) > self._max_entries:
                ordered = sorted(self._data.items(), key=lambda kv: kv[1].get("updated_at", ""))
                for key, _ in ordered[: len(self._data) - self._max_entries]:
                    del self._data[key]
            self._save()

    def forget(self, channel_id: str, thread_ts: str) -> None:
        with self._lock:
            if self._data.pop(self._key(channel_id, thread_ts), None) is not None:
                self._save()
