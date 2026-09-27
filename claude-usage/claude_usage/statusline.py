"""Claude Code の statusline エントリポイント。

stdin で渡される JSON を記録し、1行を標準出力へ返す。

    python -m claude_usage.statusline

このスクリプトが**公式の利用枠の唯一の入口**。任意のタイミングで取得できないので、
渡されたときに必ず記録しておき、``claude-usage`` はその記録を読む。

表示は「上限に当てない・余らせない」に必要な順に並べる。

    Opus | ctx 62% | 5h 78% (reset 1:12) | 7d 49%

* ``5h`` … 使い切るとリセットまで作業できない。残り時間（``↺``）が要る
* ``7d`` … 余らせるともったいない。ペース判定は CLI 側で出す

**絶対に例外で落ちないこと。** statusline が失敗すると利用者の画面が壊れる。
値が取れなければ黙って省く。出力する文字も、端末の文字コードで書けるものだけに
落とす（``display`` を参照。cp932 で ``↺`` を出そうとして空行になった実例がある）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from claude_usage import display
from claude_usage import snapshot as snap


def _duration(seconds: float | None) -> str:
    """``1:12``（1時間12分）や ``12m``。リセットまでの残りに使う。"""
    if seconds is None:
        return ""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}:{minutes % 60:02d}"


def render(shot: snap.Snapshot, now: datetime | None = None) -> str:
    """statusline の1行。取れなかった値は省く。"""
    parts: list[str] = []

    if shot.model:
        parts.append(shot.model)
    if shot.context_used_percent is not None:
        parts.append(f"ctx {shot.context_used_percent:.0f}%")

    if shot.five_hour.available:
        text = f"5h {shot.five_hour.used_percent:.0f}%"
        remaining = _duration(shot.five_hour.seconds_until_reset(now))
        if remaining:
            # リセットまでの残り時間。これが無いと「あと何分で復活するか」が分からない。
            text += f" ({display.reset_mark()} {remaining})"
        parts.append(text)

    if shot.seven_day.available:
        parts.append(f"7d {shot.seven_day.used_percent:.0f}%")

    if shot.spend_limit.available:
        parts.append(f"spend {shot.spend_limit.used_percent:.0f}%")

    if not shot.has_official_limits:
        # 公式値が入ってこない環境だと分かるようにする。黙って context だけ出すと
        # 「利用枠を見ている」と誤解される。
        parts.append("limits n/a")

    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    raw_text = sys.stdin.read()
    try:
        payload = json.loads(raw_text) if raw_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    now = datetime.now(timezone.utc)
    shot = snap.parse(payload, now=now)
    snap.save(shot, raw=payload)
    display.write_line(render(shot, now=now))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - statusline は何があっても落とさない
        # 空行でも statusline としては成立する。落ちて画面が壊れるより良い。
        raise SystemExit(0)
