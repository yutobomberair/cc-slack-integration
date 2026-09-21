"""多重起動の防止。

ブリッジが複数同時に動くと、次の問題がまとめて起きる。

* Slack はイベントを接続のどれか1本にだけ配る。どのインスタンスが受けるかは
  不定になり、スレッドの連続した依頼が別プロセスに散る。
* ``SessionStore`` はプロセス内にも状態を持つため、別プロセスの記録が見えず
  「継続すべきスレッドを新規セッション扱い」してしまう。
* ``ThreadLocks`` はプロセス内のロックなので、直列化が効かなくなる。
* Claude Code が二重に走り、課金も倍になる。

実際にこの状態が起きた（タスク停止時に python の子プロセスが生き残り、3インスタンスが
並走した）ため、プロセス側でも歯止めを入れる。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """別のインスタンスが既に動いている。"""


def _pid_alive(pid: int) -> bool:
    """Windows / POSIX どちらでも動く生存確認。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess

        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        return f'"{pid}"' in (completed.stdout or "")
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


class InstanceLock:
    """PID を書いたロックファイル。

    ファイルが残っていても、その PID が生きていなければ奪って起動する
    （クラッシュ後に二度と起動できなくなるのを避けるため）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._acquired = False

    def acquire(self) -> None:
        if self._path.exists():
            try:
                existing = int(self._path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                existing = 0
            # 同一 PID を例外扱いしない。二重取得に正当な用途は無く、例外にすると
            # 「生きているロックがあるのに素通りする」経路ができてしまう。
            if existing and _pid_alive(existing):
                raise AlreadyRunningError(
                    f"別の Slack Bridge が既に動作しています (pid={existing})。\n"
                    "停止するには scripts\\stop_bridge.ps1 を実行してください。\n"
                    f"（ロックファイル: {self._path}）"
                )
            logger.info("古いロックファイルを引き継ぎます (pid=%s は不在)", existing)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(os.getpid()), encoding="utf-8")
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self._path.exists():
                current = self._path.read_text(encoding="utf-8").strip()
                if current == str(os.getpid()):
                    self._path.unlink()
        except OSError:
            logger.debug("ロックファイルの削除に失敗しました", exc_info=True)
        self._acquired = False

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
