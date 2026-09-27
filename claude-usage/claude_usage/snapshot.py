"""statusline から渡される JSON をスナップショットとして読み書きする。

Claude Code の 5時間枠 / 7日枠の利用率は、**statusline スクリプトの stdin にしか
流れてこない**。任意のタイミングで引く手段が無いので、渡されたときに必ず記録し、
CLI はその記録を読む。

    Claude Code ──(stdin JSON)──> statusline ──> スナップショット ──> CLI

したがって「Claude Code を使っていない間は更新されない」。使っていなければ消費も
されないので実害は小さいが、**古い値を現在値として見せない**よう、読み出し側は必ず
``captured_at`` を確認して表示すること。

Phase 1 では追記のみの JSONL に貯める。SQLite は Phase 2（履歴の問い合わせが必要に
なってから）。JSONL でも消費速度の算出に足りる。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def data_dir() -> Path:
    """記録の置き場。``CLAUDE_USAGE_HOME`` で差し替えられる（テスト用）。"""
    override = os.environ.get("CLAUDE_USAGE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude-usage"


@dataclass(frozen=True)
class Window:
    """利用枠ひとつ分。公式値が無ければ ``used_percent`` は None。"""

    used_percent: float | None = None
    resets_at: int | None = None       # Unix epoch 秒

    @property
    def available(self) -> bool:
        return self.used_percent is not None

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)

    @property
    def reset_datetime(self) -> datetime | None:
        if self.resets_at is None:
            return None
        return datetime.fromtimestamp(self.resets_at, tz=timezone.utc)

    def seconds_until_reset(self, now: datetime | None = None) -> float | None:
        reset = self.reset_datetime
        if reset is None:
            return None
        current = now or datetime.now(timezone.utc)
        return max(0.0, (reset - current).total_seconds())


@dataclass(frozen=True)
class Snapshot:
    """ある時点の利用状況。

    ``five_hour`` と ``seven_day`` が公式値（Confidence: official）。
    それ以外はセッション固有の情報で、利用枠とは別の指標（仕様書 §2.1）。
    """

    captured_at: str                       # ISO8601（UTC）
    five_hour: Window = field(default_factory=Window)
    seven_day: Window = field(default_factory=Window)
    spend_limit: Window = field(default_factory=Window)

    model: str | None = None
    context_used_percent: float | None = None
    context_size: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    session_cost_usd: float | None = None

    session_id: str | None = None
    project_dir: str | None = None
    version: str | None = None

    @property
    def captured_datetime(self) -> datetime:
        return datetime.fromisoformat(self.captured_at)

    @property
    def has_official_limits(self) -> bool:
        """公式の利用枠が取れているか。

        取れていなければ Official Provider は成立しない（認証方式や
        Claude Code のバージョンによっては入ってこない可能性がある）。
        """
        return self.five_hour.available or self.seven_day.available


def _num(value) -> float | None:
    """数値として使えるものだけ通す。文字列や None は捨てる。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch(value) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _window(raw: dict, key: str) -> Window:
    body = raw.get(key)
    if not isinstance(body, dict):
        return Window()
    return Window(
        used_percent=_num(body.get("used_percentage")),
        resets_at=_epoch(body.get("resets_at")),
    )


def parse(payload: dict, now: datetime | None = None) -> Snapshot:
    """statusline の JSON を Snapshot にする。

    欠けているフィールドは None にする。Claude Code の更新でフィールドが増減しても
    落ちないように、型が合わないものは黙って捨てる（statusline が例外で落ちると
    利用者の画面が壊れるため）。
    """
    limits = payload.get("rate_limits")
    limits = limits if isinstance(limits, dict) else {}
    context = payload.get("context_window")
    context = context if isinstance(context, dict) else {}
    cost = payload.get("cost")
    cost = cost if isinstance(cost, dict) else {}
    model = payload.get("model")
    model = model if isinstance(model, dict) else {}
    workspace = payload.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}

    return Snapshot(
        captured_at=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        five_hour=_window(limits, "five_hour"),
        seven_day=_window(limits, "seven_day"),
        spend_limit=_window(limits, "spend_limit"),
        model=model.get("display_name") or model.get("id"),
        context_used_percent=_num(context.get("used_percentage")),
        context_size=_epoch(context.get("context_window_size")),
        input_tokens=_epoch(context.get("total_input_tokens")),
        output_tokens=_epoch(context.get("total_output_tokens")),
        session_cost_usd=_num(cost.get("total_cost_usd")),
        session_id=payload.get("session_id"),
        project_dir=workspace.get("project_dir") or payload.get("cwd"),
        version=payload.get("version"),
    )


# --------------------------------------------------------------------------
# 保存と読み出し
# --------------------------------------------------------------------------

def _history_path() -> Path:
    return data_dir() / "snapshots.jsonl"


def _latest_path() -> Path:
    return data_dir() / "latest.json"


def changed(previous: Snapshot | None, current: Snapshot) -> bool:
    """履歴に足す価値があるか。

    statusline は数百ミリ秒ごとに呼ばれうる。利用率が動いていないスナップショットを
    貯めても消費速度の計算に寄与せず、ファイルが膨らむだけなので弾く。
    """
    if previous is None:
        return True
    return (
        previous.five_hour.used_percent != current.five_hour.used_percent
        or previous.seven_day.used_percent != current.seven_day.used_percent
        or previous.five_hour.resets_at != current.five_hour.resets_at
        or previous.seven_day.resets_at != current.seven_day.resets_at
    )


def save(snapshot: Snapshot, raw: dict | None = None) -> None:
    """最新値を上書きし、利用率が動いていれば履歴にも足す。

    書き込みに失敗しても例外を投げない。statusline が落ちると利用者の画面が
    壊れるので、記録の失敗より表示の継続を優先する。
    """
    try:
        directory = data_dir()
        directory.mkdir(parents=True, exist_ok=True)

        previous = load_latest()
        body = asdict(snapshot)
        if raw is not None:
            # フィールドが増えたときに後から追えるよう生データも残す。
            # statusline の JSON に認証情報は含まれない（仕様書 §17 を確認済み）。
            body["raw"] = raw

        tmp = directory / "latest.json.tmp"
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_latest_path())

        if changed(previous, snapshot):
            with _history_path().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(body, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _from_dict(body: dict) -> Snapshot:
    def window(key: str) -> Window:
        raw = body.get(key) or {}
        return Window(
            used_percent=raw.get("used_percent"), resets_at=raw.get("resets_at")
        )

    known = {f for f in Snapshot.__dataclass_fields__ if f not in
             ("five_hour", "seven_day", "spend_limit")}
    return Snapshot(
        five_hour=window("five_hour"),
        seven_day=window("seven_day"),
        spend_limit=window("spend_limit"),
        **{k: body.get(k) for k in known},
    )


def load_latest() -> Snapshot | None:
    try:
        body = json.loads(_latest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    try:
        return _from_dict(body)
    except (TypeError, ValueError):
        return None


def load_history(limit: int = 500) -> list[Snapshot]:
    """新しい順ではなく**古い順**に返す。消費速度は時系列で見るため。"""
    try:
        lines = _history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[Snapshot] = []
    for line in lines[-limit:]:
        try:
            out.append(_from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return out
