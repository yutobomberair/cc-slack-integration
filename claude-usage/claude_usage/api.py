"""他のプログラムから呼ぶための口。

CLI の中身（``cli.py``）は ``argparse`` と標準出力に結び付いているので、外から使う
にはそこを迂回する必要がある。ここが唯一の入口。

用途はいまのところ Slack ブリッジ。スマホから「いま何%？」を聞けないと、通知は
届くのに自分からは確認できない、という歪んだ状態になる。

    from claude_usage import api
    text = api.report("summary")
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from claude_usage import cli
from claude_usage import snapshot as snap

#: 受け付けるコマンド。CLI の引数とそろえる。
COMMANDS = ("summary", "status", "projects", "alerts", "json")

_ALIASES = {
    "": "summary",
    "now": "summary",
    "要点": "summary",
    "詳細": "status",
    "detail": "status",
    "project": "projects",
    "プロジェクト": "projects",
    "alert": "alerts",
    "通知": "alerts",
}

NO_DATA = (
    "スナップショットがまだありません。\n"
    "Claude Code の statusline が動くと記録されます。"
)


def normalise(command: str | None) -> str | None:
    """別名を正規の名前に。知らない語は None。"""
    name = (command or "").strip().lower()
    name = _ALIASES.get(name, name)
    return name if name in COMMANDS else None


def report(command: str = "summary") -> str:
    """指定のレポートを文字列で返す。

    CLI と同じ内容を返す。データが無ければその旨を返す（例外は投げない。
    呼び出し側が Slack へ返す文面としてそのまま使えるようにする）。
    """
    name = normalise(command)
    if name is None:
        listed = " / ".join(COMMANDS)
        return f"`{command}` は指定できません。使えるのは: {listed}"

    shot = snap.load_latest()
    if shot is None:
        return NO_DATA

    history = snap.load_history()
    now = datetime.now(timezone.utc)

    if name == "json":
        return json.dumps(cli.build_json(shot, history, now), ensure_ascii=False, indent=2)
    if name == "status":
        return cli.render_status(shot, history, now)
    if name == "projects":
        return cli.render_projects(shot, days=None, by_repo=True)
    if name == "alerts":
        return cli.render_alerts(shot, history, now)
    return cli.render_summary(shot, history, now)
