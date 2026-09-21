"""channel_id からプロジェクトを特定する（CLAUDE.md §4, §15）。

未登録チャンネルでは絶対に Claude Code を起動しない。ここが唯一の判定点になる。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Project, Settings


@dataclass(frozen=True)
class RouteResult:
    project: Project | None
    channel_id: str

    @property
    def is_known(self) -> bool:
        return self.project is not None


def route(settings: Settings, channel_id: str) -> RouteResult:
    return RouteResult(project=settings.project_for_channel(channel_id), channel_id=channel_id)


def unknown_channel_message(channel_id: str) -> str:
    """未登録チャンネルへ返す案内文（CLAUDE.md §15）。"""
    return (
        ":warning: このチャンネルには Claude Code のプロジェクトが設定されていません。\n\n"
        f"channel_id:\n`{channel_id}`\n\n"
        "`config/projects.yaml` にこの channel_id を追加してから Slack Bridge を再起動してください。"
    )
