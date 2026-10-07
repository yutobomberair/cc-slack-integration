"""スレッドごとの設定（作業階層・モデル・推論の強さ）。

``cd:`` の記録を一般化したもの。3つとも「このスレッドでは何を使うか」という同じ
性質の設定なので、1つの記録にまとめる。別々のファイルに分けると、スレッドの設定を
見るのに複数のファイルを突き合わせることになる。

設定の優先順はこう。下へ行くほど弱い。

    1. 依頼文のインライン指定   max opus: 直して
    2. スレッドの設定           model: sonnet       ← ここが本モジュール
    3. プロジェクトの既定       projects.yaml の project.model / effort
    4. runtime の既定           projects.yaml の runtime.model / effort
    5. Claude Code の既定       何も渡さない

項目ごとに設定・解除できる。``cd: .`` で階層を直下に戻したときにモデルの設定まで
消えてしまう、ということを起こさないため。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: 記録する項目。増やすならここへ足す。
FIELDS = ("workdir", "model", "effort")


class ThreadPrefs:
    """スレッド -> 設定。

    ブリッジを再起動しても残るようファイルへ落とす（``SessionStore`` と同じ方針）。
    全項目が空になった記録は捨てる（既定と同じ状態を残す意味が無い）。
    """

    def __init__(self, path: Path, max_entries: int = 300) -> None:
        self._path = path
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            body = json.loads(self._path.read_text(encoding="utf-8"))
            return body if isinstance(body, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("スレッド設定を読めませんでした。空から開始します: %s", self._path)
            return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except OSError:
            logger.exception("スレッド設定の保存に失敗しました: %s", self._path)

    @staticmethod
    def _key(channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def all(self, channel_id: str, thread_ts: str) -> dict[str, str]:
        """このスレッドの設定。未設定の項目は含めない。"""
        with self._lock:
            self._data = self._load()
            entry = self._data.get(self._key(channel_id, thread_ts))
            if not isinstance(entry, dict):
                return {}
            return {
                name: str(entry[name])
                for name in FIELDS
                if entry.get(name)
            }

    def get(self, channel_id: str, thread_ts: str, field: str = "workdir") -> str:
        """1項目を取る。未設定なら空文字。"""
        return self.all(channel_id, thread_ts).get(field, "")

    def set(self, channel_id: str, thread_ts: str, project_key: str, **values) -> None:
        """項目を設定する。``None`` か空文字を渡した項目は解除する。

        項目ごとに扱うのが要点。``cd: .`` で階層を戻したときに、同じスレッドで
        設定していたモデルまで消える、ということを起こさない。
        """
        unknown = set(values) - set(FIELDS)
        if unknown:
            raise ValueError(f"知らない項目です: {sorted(unknown)}")

        with self._lock:
            self._data = self._load()
            key = self._key(channel_id, thread_ts)
            entry = dict(self._data.get(key) or {})
            for name, value in values.items():
                if value:
                    entry[name] = str(value)
                else:
                    entry.pop(name, None)

            if any(entry.get(name) for name in FIELDS):
                entry["project"] = project_key
                entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._data[key] = entry
            else:
                # 全部が既定に戻ったら記録を持たない
                self._data.pop(key, None)

            if len(self._data) > self._max_entries:
                ordered = sorted(
                    self._data.items(), key=lambda kv: kv[1].get("updated_at", "")
                )
                for stale, _ in ordered[: len(self._data) - self._max_entries]:
                    del self._data[stale]
            self._save()
