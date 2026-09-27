"""スレッドごとの作業階層（``cd:``）。

Claude Code は起動時の cwd から ``.claude/`` を探すので、プロジェクト内に開発ルールが
複数階層あると、どこで起動するかで読まれる CLAUDE.md と skills が変わる。

    propose/
    ├── .claude/CLAUDE.md, skills/      ← propose で起動すると読まれる
    └── movie/
        └── .claude/CLAUDE.md, skills/  ← propose/movie で起動すると読まれる

チャンネルごとに1つのパスに固定していると後者を使えないため、スレッド単位で
配下の階層を選べるようにする。

**プロジェクト配下に限る。** ``..`` や絶対パスは弾く。ここに Slack からの入力が
そのまま通ると、``config/projects.yaml`` による範囲指定の歯止めが効かなくなる
（CLAUDE.md §15 の「未登録チャンネルでは実行しない」と同じ趣旨）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

#: プロジェクト直下を指す表記。``cd: .`` で戻す。
ROOT_TOKENS = {".", "/", "~", "root", "ルート", "直下"}

_DIRECTIVE_TOKENS = ("cd", "dir", "/cd", "階層", "移動")


class WorkdirError(ValueError):
    """指定された階層が使えない。"""


def parse_directive(prompt: str) -> str | None:
    """``cd: movie`` なら ``"movie"`` を返す。``cd:`` だけなら空文字。

    先頭行だけを見る。本文中の ``cd:`` に反応すると、この機能自体について
    相談したいときに Claude へ渡らなくなる（``共有:`` と同じ方針）。
    """
    head = (prompt or "").strip().splitlines()[0] if (prompt or "").strip() else ""
    for separator in (":", "："):
        key, sep, value = head.partition(separator)
        if sep and key.strip().lower() in _DIRECTIVE_TOKENS:
            return value.strip()
    return None


def resolve(project_root: Path, requested: str) -> Path:
    """``requested`` をプロジェクト配下の実ディレクトリに解決する。

    受け付けるのは配下の相対パスだけ。外に出る指定と存在しない階層は弾く。
    """
    root = Path(project_root).resolve()
    text = (requested or "").strip().strip("`\"'").replace("\\", "/")

    if not text or text in ROOT_TOKENS:
        return root

    candidate = PurePosixPath(text)
    if candidate.is_absolute() or ":" in text:
        raise WorkdirError(
            "絶対パスは指定できません。プロジェクト配下の相対パスで指定してください。"
        )
    if ".." in candidate.parts:
        raise WorkdirError("`..` でプロジェクトの外へは移動できません。")

    target = (root / candidate).resolve()
    # resolve 後にもう一度確かめる。シンボリックリンク経由で外へ出る場合を防ぐ。
    if target != root and root not in target.parents:
        raise WorkdirError("プロジェクトの外へは移動できません。")
    if not target.is_dir():
        raise WorkdirError(f"`{text}` というディレクトリはありません。")
    return target


def relative_label(project_root: Path, workdir: Path) -> str:
    """表示用の相対表記。プロジェクト直下なら空文字。"""
    try:
        rel = Path(workdir).resolve().relative_to(Path(project_root).resolve())
    except ValueError:
        return str(workdir)
    return "" if str(rel) == "." else rel.as_posix()


def candidates(project_root: Path, limit: int = 12) -> list[str]:
    """``.claude/`` を持つ配下の階層。``cd:`` を間違えたときの案内に使う。

    開発ルールがある場所こそが移動先として意味を持つので、それを挙げる。
    """
    root = Path(project_root).resolve()
    found: list[str] = []
    try:
        for entry in sorted(root.rglob(".claude")):
            if not entry.is_dir():
                continue
            label = relative_label(root, entry.parent)
            if label and ".claude" not in label:
                found.append(label)
            if len(found) >= limit:
                break
    except OSError:
        logger.debug("階層の探索に失敗しました: %s", root, exc_info=True)
    return found


# --------------------------------------------------------------------------
# 返信の文面
# --------------------------------------------------------------------------

def changed_message(project_name: str, label: str) -> str:
    where = f"`{label}/`" if label else "プロジェクト直下"
    return (
        f":file_folder: このスレッドの作業階層を {where} にしました。\n"
        f"Project: *{project_name}*\n\n"
        "以降このスレッドの依頼はこの階層で実行します（`cd: .` で戻せます）。"
    )


def current_message(project_name: str, label: str, options: list[str]) -> str:
    where = f"`{label}/`" if label else "プロジェクト直下"
    text = (
        f":file_folder: 現在の作業階層は {where} です。\n"
        f"Project: *{project_name}*"
    )
    if options:
        listed = " / ".join(f"`{o}`" for o in options)
        text += f"\n\n開発ルールがある階層: {listed}"
    return text


def error_message(project_name: str, reason: str, options: list[str]) -> str:
    text = f":thinking_face: {reason}\nProject: *{project_name}*"
    if options:
        listed = " / ".join(f"`{o}`" for o in options)
        text += f"\n\n指定できる階層: {listed}"
    return text


# --------------------------------------------------------------------------
# スレッドごとの記録
# --------------------------------------------------------------------------

class WorkdirStore:
    """スレッド -> 選ばれた階層（プロジェクト直下からの相対表記）。

    ブリッジを再起動しても選択が残るようファイルへ落とす
    （``SessionStore`` / ``ArtifactStore`` と同じ方針）。
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
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("作業階層の記録を読めませんでした。空から開始します: %s", self._path)
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
            logger.exception("作業階層の記録の保存に失敗しました: %s", self._path)

    @staticmethod
    def _key(channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def get(self, channel_id: str, thread_ts: str) -> str:
        """記録されている相対表記。無ければ空文字（プロジェクト直下）。"""
        with self._lock:
            self._data = self._load()
            entry = self._data.get(self._key(channel_id, thread_ts))
            return str(entry.get("workdir", "")) if entry else ""

    def set(self, channel_id: str, thread_ts: str, project_key: str, label: str) -> None:
        with self._lock:
            self._data = self._load()
            key = self._key(channel_id, thread_ts)
            if not label:
                # 直下に戻したら記録を持たない。既定と同じ状態を残す意味が無い。
                self._data.pop(key, None)
            else:
                self._data[key] = {
                    "project": project_key,
                    "workdir": label,
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            if len(self._data) > self._max_entries:
                ordered = sorted(self._data.items(), key=lambda kv: kv[1].get("updated_at", ""))
                for stale, _ in ordered[: len(self._data) - self._max_entries]:
                    del self._data[stale]
            self._save()
