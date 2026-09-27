"""ペース判定 — この道具の目的そのもの。

知りたいのは「いま何%か」ではなく次の2つ。

* **5時間枠**: このまま続けて、リセット前に使い切って止められないか
* **7日枠**: 余らせていないか、逆に週内に尽きそうか

だから警告は**両側**に要る。使いすぎだけでなく、使い残しも知らせる。
閾値（70/85/95）だけでは後者を拾えない。

## 消費速度の出し方

スナップショットの差分から求める。ただし枠のリセットを跨いだ区間は使えない。
リセットすると ``used_percent`` が下がり、``resets_at`` が次の時刻に変わるので、
そこで履歴を切る。

## 何を「使い残し」と呼ぶか

7日枠の残量を残り時間で割ったものが、これから使える1日あたりの量（基準ペース）。
実績がそれを大きく下回っていれば、同じ調子では余る。

    基準ペース = 残量% ÷ 残り日数
    実績ペース = 直近の消費%  ÷ その経過日数

基準より速ければ週内に尽き、遅ければ余る。どちらも知らせる価値がある。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from claude_usage.snapshot import Snapshot

#: 実績ペースがこの倍率を下回ったら「使い残し」とみなす。
#: 1.0 ちょうどを基準にすると誤差で揺れるので、余裕を見て 0.6。
UNDERUSE_RATIO = 0.6

#: 実績ペースがこの倍率を超えたら「使いすぎ」とみなす。
OVERUSE_RATIO = 1.25

#: 消費速度を出すのに必要な最小の経過時間（秒）。短すぎる区間は誤差が大きい。
MIN_SPAN_SECONDS = 300.0


@dataclass(frozen=True)
class Burn:
    """ある枠の消費速度と、それによる見通し。"""

    percent_per_hour: float | None = None
    #: この速度で使い切る時刻。リセットより後なら枠内に収まる。
    exhausts_at: datetime | None = None
    #: 速度を求めるのに使った区間の長さ（秒）。短いほど当てにならない。
    span_seconds: float | None = None

    @property
    def measured(self) -> bool:
        return self.percent_per_hour is not None


def _window_of(shot: Snapshot, key: str):
    return getattr(shot, key)


def _same_window(a: Snapshot, b: Snapshot, key: str) -> bool:
    """2つのスナップショットが同じ枠の中にあるか。

    ``resets_at`` が変われば別の枠。``used_percent`` が下がった場合も、
    リセットされたと見て別扱いにする（``resets_at`` の更新が遅れる場合の保険）。
    """
    first, second = _window_of(a, key), _window_of(b, key)
    if first.resets_at != second.resets_at:
        return False
    if first.used_percent is None or second.used_percent is None:
        return False
    return second.used_percent >= first.used_percent


def current_window_history(history: list[Snapshot], key: str) -> list[Snapshot]:
    """いまの枠に属する分だけを、古い順で返す。

    末尾から遡り、枠が変わったところで切る。跨いだ区間で速度を出すと、
    リセット直後に「消費が減った」ことになって見通しが壊れる。
    """
    usable = [s for s in history if _window_of(s, key).available]
    if not usable:
        return []
    out = [usable[-1]]
    for older in reversed(usable[:-1]):
        if not _same_window(older, out[0], key):
            break
        out.insert(0, older)
    return out


def burn(history: list[Snapshot], key: str, now: datetime | None = None) -> Burn:
    """いまの枠での消費速度と、使い切る時刻の見通し。"""
    window_history = current_window_history(history, key)
    if len(window_history) < 2:
        return Burn()

    first, last = window_history[0], window_history[-1]
    span = (last.captured_datetime - first.captured_datetime).total_seconds()
    if span < MIN_SPAN_SECONDS:
        return Burn(span_seconds=span)

    consumed = _window_of(last, key).used_percent - _window_of(first, key).used_percent
    rate = consumed / (span / 3600.0)
    if rate <= 0:
        # 消費が進んでいない。使い切る見通しは立たない（立てる必要もない）。
        return Burn(percent_per_hour=0.0, span_seconds=span)

    remaining = _window_of(last, key).remaining_percent or 0.0
    current = now or datetime.now(timezone.utc)
    return Burn(
        percent_per_hour=rate,
        exhausts_at=current + timedelta(hours=remaining / rate),
        span_seconds=span,
    )


@dataclass(frozen=True)
class Verdict:
    """ペースの判定。``level`` で表示の強さを決める。"""

    level: str      # "ok" / "underuse" / "warning" / "critical" / "unknown"
    headline: str
    detail: str = ""

    @property
    def is_problem(self) -> bool:
        return self.level in ("warning", "critical")


def _hhmm(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def five_hour_verdict(
    shot: Snapshot, history: list[Snapshot], now: datetime | None = None
) -> Verdict:
    """5時間枠の判定。関心は「リセット前に止められないか」の一点。"""
    window = shot.five_hour
    if not window.available:
        return Verdict("unknown", "5時間枠の利用率が取得できていません")

    current = now or datetime.now(timezone.utc)
    reset_text = _hhmm(window.reset_datetime) if window.reset_datetime else "不明"
    remaining = window.remaining_percent or 0.0

    if remaining <= 0:
        return Verdict(
            "critical",
            f"5時間枠を使い切っています。{reset_text} まで待つ必要があります",
        )

    measured = burn(history, "five_hour", current)
    if measured.exhausts_at is not None and measured.exhausts_at < (
        window.reset_datetime or current
    ):
        return Verdict(
            "warning",
            f"このペースだと {_hhmm(measured.exhausts_at)} に5時間枠を使い切ります",
            f"リセットは {reset_text}（{measured.percent_per_hour:.1f}%/時 で消費中）",
        )

    if remaining < 15:
        return Verdict(
            "warning",
            f"5時間枠の残りが {remaining:.0f}% です",
            f"リセットは {reset_text}",
        )

    return Verdict(
        "ok",
        f"5時間枠は残り {remaining:.0f}%（リセット {reset_text}）",
        f"{measured.percent_per_hour:.1f}%/時 で消費中" if measured.measured else "",
    )


def seven_day_verdict(
    shot: Snapshot, history: list[Snapshot], now: datetime | None = None
) -> Verdict:
    """7日枠の判定。使いすぎと使い残しの両方を見る。"""
    window = shot.seven_day
    if not window.available:
        return Verdict("unknown", "7日枠の利用率が取得できていません")

    current = now or datetime.now(timezone.utc)
    seconds_left = window.seconds_until_reset(current)
    remaining = window.remaining_percent or 0.0

    if remaining <= 0:
        reset = window.reset_datetime
        when = reset.astimezone().strftime("%m-%d %H:%M") if reset else "不明"
        return Verdict("critical", f"7日枠を使い切っています。{when} まで回復しません")

    if not seconds_left:
        return Verdict("ok", f"7日枠は残り {remaining:.0f}%")

    days_left = seconds_left / 86400.0
    budget_per_day = remaining / days_left if days_left > 0 else remaining

    measured = burn(history, "seven_day", current)
    if not measured.measured or measured.percent_per_hour == 0:
        return Verdict(
            "ok",
            f"7日枠は残り {remaining:.0f}%（あと {days_left:.1f} 日）",
            f"1日あたり {budget_per_day:.1f}% まで使えます",
        )

    actual_per_day = measured.percent_per_hour * 24.0

    if actual_per_day > budget_per_day * OVERUSE_RATIO:
        return Verdict(
            "warning",
            "7日枠の消費が速すぎます"
            f"（実績 {actual_per_day:.1f}%/日 > 目安 {budget_per_day:.1f}%/日）",
            f"このままだと残り {days_left:.1f} 日を待たずに尽きます",
        )

    if actual_per_day < budget_per_day * UNDERUSE_RATIO:
        return Verdict(
            "underuse",
            "7日枠を使い残しそうです"
            f"（実績 {actual_per_day:.1f}%/日 < 目安 {budget_per_day:.1f}%/日）",
            f"あと {days_left:.1f} 日で残り {remaining:.0f}%。"
            f"1日 {budget_per_day:.1f}% まで使えます",
        )

    return Verdict(
        "ok",
        f"7日枠は残り {remaining:.0f}%（あと {days_left:.1f} 日）",
        f"実績 {actual_per_day:.1f}%/日 ／ 目安 {budget_per_day:.1f}%/日",
    )


def overall(verdicts: list[Verdict]) -> Verdict:
    """まとめの1行。重い判定を優先する。"""
    for level in ("critical", "warning", "underuse"):
        for verdict in verdicts:
            if verdict.level == level:
                return verdict
    if all(v.level == "unknown" for v in verdicts):
        return Verdict("unknown", "利用枠の情報がありません")
    return Verdict("ok", "ペースは問題ありません")
