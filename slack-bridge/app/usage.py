"""Slack から利用枠を聞く（``usage``）。

通知は ``#all-claude-code`` へ届くのに、こちらから聞く手段が無かった。スマホから
「いま何%？」を確認できないと、残量を意識して作業するという目的が半分しか果たせない。

``claude-usage`` の CLI と同じ内容を返す。

    @ClaudeCode usage            要点とペース判定
    @ClaudeCode usage status     詳細（値ごとの Confidence 付き）
    @ClaudeCode usage projects   どのプロジェクトが重いか
    @ClaudeCode usage alerts     いまの判定と、鳴った履歴
    @ClaudeCode usage json       機械可読

``共有:`` や ``cd:`` と同じくブリッジが直接処理する。Claude Code は起動しないので
課金もセッション更新も発生せず、即座に返る。

``claude-usage`` は同じリポジトリの隣にあるが、**依存は任意**にしてある。無くても
ブリッジは動き、``usage`` だけが使えない旨を返す。標準ライブラリしか使っていない
ので、import できるように sys.path を通すだけで済む（別の venv は要らない）。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

#: ``claude-usage`` の置き場。既定はリポジトリ内の隣。環境変数で差し替えられる。
DEFAULT_PATH = BASE_DIR.parent / "claude-usage"

_DIRECTIVE_TOKENS = ("usage", "/usage", "使用量", "残量", "利用量")

#: Slack の1メッセージに収まる範囲。超える分は formatting 側で分割される。
_CODE_FENCE = "```"


def project_path() -> Path:
    override = os.environ.get("CLAUDE_USAGE_PATH", "").strip()
    return Path(override) if override else DEFAULT_PATH


def parse_directive(prompt: str) -> str | None:
    """``usage`` なら対象（空文字なら要点）を返す。``usage: status`` なら ``"status"``。

    ``ls`` と同じくコロンを省ける。先頭行だけを見るので、本文中の「usage」には
    反応しない（この機能自体について相談できなくなるため）。
    """
    text = (prompt or "").strip()
    if not text:
        return None
    head = text.splitlines()[0].strip()

    if head.lower() in _DIRECTIVE_TOKENS:
        return ""
    for separator in (":", "："):
        key, sep, value = head.partition(separator)
        if sep and key.strip().lower() in _DIRECTIVE_TOKENS:
            return value.strip()
    parts = head.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in _DIRECTIVE_TOKENS:
        return parts[1].strip()
    return None


def _api():
    """``claude_usage.api`` を読み込む。無ければ None。

    ブリッジ起動時ではなく呼ばれた時に読む。``claude-usage`` を消しても
    ブリッジが起動しなくなる、という結び付きを作らないため。
    """
    path = str(project_path())
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        from claude_usage import api  # noqa: PLC0415 - 遅延 import は意図的
        return api
    except ImportError:
        logger.warning("claude-usage を読み込めませんでした: %s", path)
        return None


def not_available_message() -> str:
    return (
        ":open_file_folder: 利用枠の情報を取得できません。\n\n"
        f"`claude-usage` が見つかりませんでした（探した場所: `{project_path()}`）。\n"
        "別の場所にある場合は環境変数 `CLAUDE_USAGE_PATH` で指定してください。"
    )


def report(command: str) -> str:
    """Slack へ返す文面。

    本文はコードブロックで囲む。バーや桁揃えが崩れると読めなくなるため。
    """
    api = _api()
    if api is None:
        return not_available_message()

    try:
        body = api.report(command or "summary")
    except Exception:  # noqa: BLE001 - 利用枠の表示でブリッジを落とさない
        logger.exception("利用枠の取得に失敗しました command=%s", command)
        return ":x: 利用枠の取得に失敗しました。詳細はブリッジのログを確認してください。"

    return f"{_CODE_FENCE}\n{body}\n{_CODE_FENCE}"
