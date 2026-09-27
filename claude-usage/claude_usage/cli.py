"""``claude-usage`` の入口（仕様書 §8, §9, §10）。

    claude-usage              要点とペース判定
    claude-usage status       詳細（Confidence 付き）
    claude-usage --json       機械可読
    claude-usage projects     プロジェクト別のトークン消費（Quota の配分ではない）
    claude-usage alerts       鳴った通知の履歴と、いまの判定
    claude-usage config       設定ファイルの雛形を書き出す
    claude-usage prune        保持期間を過ぎた履歴を捨てる

値は statusline スクリプトが記録したスナップショットから読む。**こちらから取りに
行く手段が無い**ので、Claude Code を使っていない間は更新されない。だから
**最終更新時刻を必ず出す**。古い値を現在値として見せないため。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from claude_usage import alerts as alerts_mod
from claude_usage import config as config_mod
from claude_usage import display, notifiers, pace, transcript
from claude_usage import snapshot as snap
from claude_usage.snapshot import Snapshot, Window

#: 何分より古いスナップショットを「古い」と言うか。
STALE_MINUTES = 10

_LEVEL_MARK = {
    "ok": ("✓", "OK"),
    "underuse": ("○", "--"),
    "warning": ("⚠", "!!"),
    "critical": ("✕", "XX"),
    "unknown": ("?", "?"),
}


def _mark(level: str) -> str:
    preferred, fallback = _LEVEL_MARK.get(level, ("?", "?"))
    return display.pick(preferred, fallback)


def _age_line(shot: Snapshot, now: datetime) -> str:
    """最終更新。古ければそれを明示する。"""
    age = (now - shot.captured_datetime).total_seconds()
    stamp = shot.captured_datetime.astimezone().strftime("%H:%M:%S")
    if age < 60:
        return f"Updated: {stamp}"
    minutes = int(age // 60)
    if minutes < STALE_MINUTES:
        return f"Updated: {stamp}（{minutes} 分前）"
    if minutes < 1440:
        return f"Updated: {stamp}（{minutes} 分前 — Claude Code 未使用中の値です）"
    days = minutes // 1440
    return f"Updated: {stamp}（{days} 日前 — 古い値です）"


def _window_block(title: str, window: Window, now: datetime) -> list[str]:
    if not window.available:
        # 仕様書 §20。取れない値を推測して埋めない。
        return [f"{title}", "  Unavailable", ""]
    lines = [
        title,
        f"  {display.bar(window.used_percent)} {window.used_percent:.0f}%",
        f"  Remaining: {window.remaining_percent:.0f}%",
    ]
    reset = window.reset_datetime
    if reset:
        seconds = window.seconds_until_reset(now) or 0
        lines.append(
            f"  Reset: {reset.astimezone():%m-%d %H:%M}（あと {seconds / 3600:.1f} 時間）"
        )
    lines.append("")
    return lines


def render_summary(shot: Snapshot, history: list[Snapshot], now: datetime) -> str:
    five = pace.five_hour_verdict(shot, history, now)
    seven = pace.seven_day_verdict(shot, history, now)
    summary = pace.overall([five, seven])

    lines = ["Claude Code Usage", "-" * 40, ""]
    if shot.model:
        lines += ["Model", f"  {shot.model}", ""]
    lines += _window_block("5-hour window", shot.five_hour, now)
    lines += _window_block("7-day window", shot.seven_day, now)
    if shot.spend_limit.available:
        lines += _window_block("Spend limit", shot.spend_limit, now)

    lines += ["Pace"]
    for verdict in (five, seven):
        if verdict.level == "unknown":
            continue
        lines.append(f"  {_mark(verdict.level)} {verdict.headline}")
        if verdict.detail:
            lines.append(f"    {verdict.detail}")
    lines += ["", "-" * 40, f"{_mark(summary.level)} {summary.headline}", _age_line(shot, now)]
    return "\n".join(lines)


def render_status(shot: Snapshot, history: list[Snapshot], now: datetime) -> str:
    """詳細表示。値ごとに Confidence を出す（仕様書 §5, §9）。"""
    five = pace.five_hour_verdict(shot, history, now)
    seven = pace.seven_day_verdict(shot, history, now)
    five_burn = pace.burn(history, "five_hour", now)
    seven_burn = pace.burn(history, "seven_day", now)

    lines = ["Claude Code Usage", "-" * 40, ""]

    for title, window, measured in (
        ("5h Window", shot.five_hour, five_burn),
        ("7-day Window", shot.seven_day, seven_burn),
    ):
        lines.append(title)
        if not window.available:
            lines += ["  Used       : Unavailable", "  Source     : unavailable", ""]
            continue
        lines.append(f"  Used       : {window.used_percent:.0f}%")
        lines.append(f"  Remaining  : {window.remaining_percent:.0f}%")
        if window.reset_datetime:
            lines.append(f"  Reset      : {window.reset_datetime.astimezone():%Y-%m-%d %H:%M}")
        lines.append("  Source     : official")
        if measured.measured:
            lines.append(f"  Burn       : {measured.percent_per_hour:.2f}%/時（estimated）")
            if measured.exhausts_at:
                lines.append(
                    f"  Exhausts   : {measured.exhausts_at.astimezone():%m-%d %H:%M}"
                    "（estimated）"
                )
        else:
            lines.append("  Burn       : 計測待ち（スナップショットが2点必要）")
        lines.append("")

    lines += ["Daily", "  Unavailable", "  Source     : unavailable",
              "  （Claude Code に日次制限は無い。5時間枠と7日枠で管理する）", ""]

    lines += ["Current Session"]
    lines.append(f"  Model      : {shot.model or '不明'}")
    if shot.context_used_percent is not None:
        lines.append(f"  Context    : {shot.context_used_percent:.0f}%")
    if shot.input_tokens is not None:
        lines.append(f"  Input      : {shot.input_tokens:,}")
    if shot.output_tokens is not None:
        lines.append(f"  Output     : {shot.output_tokens:,}")
    if shot.session_cost_usd is not None:
        lines.append(f"  Cost       : ${shot.session_cost_usd:.2f}")
    lines.append("  （Context は利用枠とは別の指標）")
    lines.append("")

    lines += ["Pace"]
    for verdict in (five, seven):
        lines.append(f"  {_mark(verdict.level)} {verdict.headline}")
        if verdict.detail:
            lines.append(f"    {verdict.detail}")
    lines += ["", f"Snapshots  : {len(history)} 件", _age_line(shot, now)]
    return "\n".join(lines)


def build_json(shot: Snapshot, history: list[Snapshot], now: datetime) -> dict:
    """仕様書 §10 の形。取れない値は null にし、confidence で区別する。"""

    def block(window: Window, measured: pace.Burn) -> dict:
        if not window.available:
            return {
                "used_percent": None,
                "remaining_percent": None,
                "reset_at": None,
                "confidence": "unavailable",
            }
        return {
            "used_percent": window.used_percent,
            "remaining_percent": window.remaining_percent,
            "reset_at": window.reset_datetime.isoformat() if window.reset_datetime else None,
            "confidence": "official",
            "burn_percent_per_hour": measured.percent_per_hour,
            "exhausts_at": measured.exhausts_at.isoformat() if measured.exhausts_at else None,
            "burn_confidence": "estimated" if measured.measured else "unavailable",
        }

    five = pace.five_hour_verdict(shot, history, now)
    seven = pace.seven_day_verdict(shot, history, now)
    return {
        "timestamp": now.astimezone().isoformat(timespec="seconds"),
        "captured_at": shot.captured_datetime.astimezone().isoformat(timespec="seconds"),
        "stale": (now - shot.captured_datetime).total_seconds() > STALE_MINUTES * 60,
        "subscription": {
            "five_hour": block(shot.five_hour, pace.burn(history, "five_hour", now)),
            "seven_day": block(shot.seven_day, pace.burn(history, "seven_day", now)),
            "daily": {
                "used_percent": None,
                "remaining_percent": None,
                "reset_at": None,
                "confidence": "unavailable",
                "note": "Claude Code に日次制限は存在しない",
            },
        },
        "session": {
            "model": shot.model,
            "context_used_percent": shot.context_used_percent,
            "input_tokens": shot.input_tokens,
            "output_tokens": shot.output_tokens,
            "cost_usd": shot.session_cost_usd,
            "session_id": shot.session_id,
            "project_dir": shot.project_dir,
        },
        "pace": {
            "five_hour": {"level": five.level, "message": five.headline, "detail": five.detail},
            "seven_day": {"level": seven.level, "message": seven.headline, "detail": seven.detail},
            "overall": pace.overall([five, seven]).level,
        },
        "snapshots": len(history),
    }


_NO_DATA = """スナップショットがまだありません。

statusline を設定すると、Claude Code が起動するたびに記録されます。

  ~/.claude/settings.json
  "statusLine": {
    "type": "command",
    "command": "python -m claude_usage.statusline"
  }

利用枠の値は statusline の stdin にしか流れてこないため、こちらから取りに行く
手段がありません（詳細は .claude/doc/claude_code_usage_monitor_findings.md）。"""


def render_projects(shot: Snapshot, days: int | None, by_repo: bool) -> str:
    """プロジェクト別のトークン消費（仕様書 §7）。

    既定では**いまの7日枠の期間**で集計する。暦の7日ではなく実際の枠に合わせるのは、
    「この枠でどこが重かったか」が知りたい情報だから。
    """
    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        period = f"直近 {days} 日"
    else:
        since = transcript.window_start(shot.seven_day.resets_at)
        period = "いまの7日枠" if since else "全期間"

    result = transcript.aggregate(since=since, by_repo=by_repo)
    rows = transcript.ranked(result)

    lines = ["Projects", "-" * 40, ""]
    lines.append(f"期間: {period}")
    if since:
        lines.append(f"  {since.astimezone():%Y-%m-%d %H:%M} 以降")
    lines.append(f"  {result.files} ファイル / {result.total.messages:,} 応答")
    lines.append("")

    if not rows:
        lines.append("集計できるレコードがありませんでした。")
        return "\n".join(lines)

    for path, tokens, share in rows:
        label = transcript.label_for(path)
        lines.append(f"{display.bar(share)} {share:5.1f}%  {label}")
        lines.append(
            f"             out {transcript.human(tokens.output)}"
            f" / cache新 {transcript.human(tokens.cache_creation)}"
            f" / cache読 {transcript.human(tokens.cache_read)}"
        )
    lines.append("")

    models = transcript.ranked_models(result)
    if models:
        lines.append("モデル別")
        for name, tokens, share in models:
            lines.append(f"  {share:5.1f}%  {name}  (out {transcript.human(tokens.output)})")
        lines.append("")

    lines.append("-" * 40)
    lines.append("比率は input + output + cache作成 で算出（cache読み直しは除外）。")
    lines.append("Subscription Quota はプロジェクト単位で提供されないため、")
    lines.append("これは利用枠の配分ではなく **トークン数の比率** です。")
    return "\n".join(lines)


def render_alerts(shot: Snapshot, history: list[Snapshot], now: datetime) -> str:
    """いまの判定と、鳴った履歴。閾値が妥当かを確かめるために両方出す。"""
    settings = config_mod.load()
    current = alerts_mod.decide(shot, history, settings, now)
    state = alerts_mod.load_state()

    lines = ["Alerts", "-" * 40, ""]
    lines.append(f"設定: {config_mod.config_path()}")
    lines.append(
        f"  閾値 warning {settings.thresholds.warning:.0f}% / "
        f"high {settings.thresholds.high:.0f}% / critical {settings.thresholds.critical:.0f}%"
    )
    lines.append(f"  使い残しの通知: {'有効' if settings.thresholds.underuse else '無効'}")
    enabled = [n for n in ("terminal", "file", "windows", "slack")
               if getattr(settings.notifiers, n)]
    lines.append(f"  通知先: {', '.join(enabled) if enabled else '（なし）'}")
    lines.append("")

    lines.append("いまの判定")
    if not current:
        lines.append("  鳴らすものはありません")
    for alert in current:
        lines.append(f"  [{alert.level}] {alert.label} — {alert.headline}")
        if alert.detail:
            lines.append(f"    {alert.detail}")
    lines.append("")

    lines.append("鳴らした記録（枠ごと。枠が変われば鳴らし直す）")
    if not state:
        lines.append("  まだありません")
    for window, recorded in state.items():
        if isinstance(recorded, dict):
            label = alerts_mod.WINDOW_LABELS.get(window, window)
            lines.append(f"  {label}: {recorded.get('level')}（reset={recorded.get('reset')}）")
    lines.append("")

    try:
        log = notifiers.log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        log = []
    lines.append(f"履歴 {notifiers.log_path()}")
    if not log:
        lines.append("  まだありません")
    for entry in log[-10:]:
        lines.append(f"  {entry}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-usage", description="Claude Code の利用枠を監視する"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="summary",
        choices=["summary", "status", "projects", "alerts", "config", "prune"],
    )
    parser.add_argument(
        "--days", type=int, default=None, help="projects: 集計する日数（既定は今の7日枠）"
    )
    parser.add_argument(
        "--by-cwd",
        action="store_true",
        help="projects: git リポジトリ単位にまとめず、作業ディレクトリごとに分ける",
    )
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    parser.add_argument(
        "--test-notify", action="store_true", help="通知先の動作確認（テスト通知を送る）"
    )
    args = parser.parse_args(argv)

    if args.command == "config":
        created = config_mod.write_template()
        path = config_mod.config_path()
        display.write_line(
            f"設定ファイルを作成しました: {path}" if created
            else f"設定ファイルは既にあります（上書きしません）: {path}"
        )
        return 0

    if args.command == "prune":
        settings = config_mod.load()
        removed = snap.prune(settings.retention_days)
        display.write_line(
            f"{removed} 行を削除しました（保持 {settings.retention_days} 日）"
        )
        return 0

    if args.test_notify:
        sample = alerts_mod.Alert(
            "five_hour", "warning", "これはテスト通知です", "claude-usage --test-notify"
        )
        result = notifiers.deliver([sample])
        display.write_line("送信結果: " + ", ".join(f"{k}={v}" for k, v in result.items()))
        return 0

    shot = snap.load_latest()
    if shot is None:
        if args.json:
            print(json.dumps({"error": "no_snapshot"}, ensure_ascii=False))
        else:
            display.write_line(_NO_DATA)
        return 1

    history = snap.load_history()
    now = datetime.now(timezone.utc)

    if args.json:
        print(json.dumps(build_json(shot, history, now), ensure_ascii=False, indent=2))
        return 0

    if args.command == "projects":
        display.write_line(render_projects(shot, args.days, by_repo=not args.by_cwd))
        return 0

    if args.command == "alerts":
        display.write_line(render_alerts(shot, history, now))
        return 0

    if args.command == "status":
        display.write_line(render_status(shot, history, now))
    else:
        display.write_line(render_summary(shot, history, now))

    # ペースに問題があれば終了コードで知らせる（cron や他ツールから使えるように）
    summary = pace.overall(
        [pace.five_hour_verdict(shot, history, now), pace.seven_day_verdict(shot, history, now)]
    )
    return 2 if summary.is_problem else 0


if __name__ == "__main__":
    raise SystemExit(main())
