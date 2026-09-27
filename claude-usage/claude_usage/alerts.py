"""閾値とペースの監視、そして同じことを何度も鳴らさないための状態管理（仕様書 §11）。

## なぜ daemon を作らないか

statusline が数百ミリ秒ごとに呼ばれている。つまり**監視の起点は既にある**。
別プロセスを常駐させると、二重起動・停止忘れ・Claude Code を使っていないのに
鳴る、といった問題を新たに抱えることになる。

## 何を鳴らすか

使いすぎ側（閾値）と使い残し側（ペース比）の両方。この道具の目的が
「上限に当てない・余らせない」の両方なので、片側だけでは足りない。

## 同じことを鳴らし続けない

statusline は常に呼ばれているので、素朴に閾値を見ると毎回鳴る。枠ごとに
「どのレベルまで鳴らしたか」を ``resets_at`` と一緒に覚えておき、**より重い
レベルに上がったときだけ**鳴らす。枠がリセットされたら記録を捨てる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from claude_usage import pace
from claude_usage.config import Config, Thresholds
from claude_usage.snapshot import Snapshot, Window, data_dir

#: 重さの順。添字の大きいほうが重い。
LEVELS = ["none", "underuse", "warning", "high", "critical"]

WINDOW_LABELS = {"five_hour": "5時間枠", "seven_day": "7日枠"}


def state_path():
    return data_dir() / "alert-state.json"


@dataclass(frozen=True)
class Alert:
    window: str          # "five_hour" / "seven_day"
    level: str           # LEVELS のいずれか（"none" 以外）
    headline: str
    detail: str = ""

    @property
    def label(self) -> str:
        return WINDOW_LABELS.get(self.window, self.window)

    @property
    def is_problem(self) -> bool:
        return self.level in ("warning", "high", "critical")


def threshold_level(percent: float | None, thresholds: Thresholds) -> str:
    """利用率がどの閾値に達しているか。"""
    if percent is None:
        return "none"
    if percent >= thresholds.critical:
        return "critical"
    if percent >= thresholds.high:
        return "high"
    if percent >= thresholds.warning:
        return "warning"
    return "none"


def heavier(a: str, b: str) -> str:
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


# --------------------------------------------------------------------------
# 鳴らすべきものを決める
# --------------------------------------------------------------------------

def _reset_key(window: Window) -> str:
    """枠を識別する値。これが変われば新しい枠なので、鳴らした記録を捨てる。"""
    return str(window.resets_at) if window.resets_at is not None else "unknown"


def _threshold_alert(name: str, window: Window, config: Config, now: datetime) -> Alert | None:
    level = threshold_level(window.used_percent, config.thresholds)
    if level == "none":
        return None
    label = WINDOW_LABELS.get(name, name)
    remaining = window.remaining_percent or 0.0
    detail = f"残り {remaining:.0f}%"
    reset = window.reset_datetime
    if reset:
        hours = (window.seconds_until_reset(now) or 0) / 3600.0
        detail += f" / リセット {reset.astimezone():%H:%M}（あと {hours:.1f} 時間）"
    return Alert(
        window=name,
        level=level,
        headline=f"{label}の使用率が {window.used_percent:.0f}% に達しました",
        detail=detail,
    )


def decide(
    shot: Snapshot,
    history: list[Snapshot],
    config: Config,
    now: datetime | None = None,
) -> list[Alert]:
    """いま鳴らす価値のあるものを返す（重複の抑制は ``filter_new`` で行う）。

    閾値とペース判定の両方を見て、枠ごとに重いほうを1件にまとめる。1つの枠について
    「70%に達した」と「このペースだと尽きる」を2通も出すと読まれなくなる。
    """
    if not config.alerts_enabled:
        return []
    current = now or datetime.now(timezone.utc)
    out: list[Alert] = []

    verdicts = {
        "five_hour": pace.five_hour_verdict(shot, history, current),
        "seven_day": pace.seven_day_verdict(shot, history, current),
    }

    for name in ("five_hour", "seven_day"):
        window = getattr(shot, name)
        if not window.available:
            continue

        candidates: list[Alert] = []
        threshold = _threshold_alert(name, window, config, current)
        if threshold is not None:
            candidates.append(threshold)

        verdict = verdicts[name]
        if verdict.level in ("warning", "critical"):
            candidates.append(
                Alert(name, verdict.level, verdict.headline, verdict.detail)
            )
        elif verdict.level == "underuse" and config.thresholds.underuse:
            candidates.append(Alert(name, "underuse", verdict.headline, verdict.detail))

        if not candidates:
            continue
        # 同じ枠では重いものだけを残す
        strongest = candidates[0]
        for candidate in candidates[1:]:
            if heavier(candidate.level, strongest.level) == candidate.level:
                strongest = candidate
        out.append(strongest)

    return out


# --------------------------------------------------------------------------
# 一度鳴らしたものを鳴らし続けない
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        body = json.loads(state_path().read_text(encoding="utf-8"))
        return body if isinstance(body, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        directory = data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / "alert-state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(state_path())
    except OSError:
        pass


def filter_new(shot: Snapshot, alerts: list[Alert], state: dict) -> tuple[list[Alert], dict]:
    """まだ鳴らしていないものだけを返し、更新後の状態も返す。

    判定は枠ごと。``resets_at`` が変われば新しい枠なので記録を捨てて鳴らし直す。
    同じ枠の中では、**より重いレベルに上がったときだけ**鳴らす。

    使い残し（``underuse``）も同じ扱い。枠が続いている間に何度も言う意味が無い。
    """
    updated = dict(state)
    fresh: list[Alert] = []

    for alert in alerts:
        window = getattr(shot, alert.window)
        key = _reset_key(window)
        recorded = updated.get(alert.window)
        if not isinstance(recorded, dict) or recorded.get("reset") != key:
            recorded = {"reset": key, "level": "none"}

        rose = heavier(alert.level, recorded["level"]) == alert.level
        if rose and alert.level != recorded["level"]:
            fresh.append(alert)
            recorded = {"reset": key, "level": alert.level}
        updated[alert.window] = recorded

    return fresh, updated


def pending(shot: Snapshot, history: list[Snapshot], config: Config,
            now: datetime | None = None) -> tuple[list[Alert], dict]:
    """``decide`` と ``filter_new`` をまとめたもの。状態の保存は呼び出し側で行う。"""
    alerts = decide(shot, history, config, now)
    return filter_new(shot, alerts, load_state())
