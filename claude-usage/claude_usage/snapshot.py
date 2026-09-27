"""statusline から渡される JSON をスナップショットとして読み書きする。

Claude Code の 5時間枠 / 7日枠の利用率は、**statusline スクリプトの stdin にしか
流れてこない**。任意のタイミングで引く手段が無いので、渡されたときに必ず記録し、
CLI はその記録を読む。

    Claude Code ──(stdin JSON)──> statusline ──> スナップショット ──> CLI

したがって「Claude Code を使っていない間は更新されない」。使っていなければ消費も
されないので実害は小さいが、**古い値を現在値として見せない**よう、読み出し側は必ず
``captured_at`` を確認して表示すること。

履歴は**追記のみの CSV**。SQLite は使わない。この規模（1行 150 バイト弱、90日で
数MB）では SQL の利点が出ず、CSV なら Excel でもそのまま開ける。プロジェクト別の
集計を月単位で出す段階になったら移行を検討する。

``raw``（statusline の生 JSON）は ``latest.json`` にだけ残す。フィールドが増えたときに
追うのが目的で、履歴の全行に同じ構造を持たせる意味は無い。当初は履歴にも入れていたが、
1行の 79% を占め 90日で 107MB になる見込みだったので外した。
"""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
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
# 置き場
# --------------------------------------------------------------------------

def _history_path() -> Path:
    return data_dir() / "snapshots.csv"


def _latest_path() -> Path:
    return data_dir() / "latest.json"


def _lock_path() -> Path:
    return data_dir() / "history.lock"


#: 履歴の列。順序を変えないこと（既存ファイルを読めなくなる）。増やすのは末尾へ。
COLUMNS = [
    "captured_at",
    "five_hour_used_percent",
    "five_hour_resets_at",
    "seven_day_used_percent",
    "seven_day_resets_at",
    "model",
    "context_used_percent",
    "input_tokens",
    "output_tokens",
    "session_cost_usd",
    "session_id",
    "project_dir",
]


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------

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


def _row(snapshot: Snapshot) -> list:
    return [
        snapshot.captured_at,
        snapshot.five_hour.used_percent,
        snapshot.five_hour.resets_at,
        snapshot.seven_day.used_percent,
        snapshot.seven_day.resets_at,
        snapshot.model,
        snapshot.context_used_percent,
        snapshot.input_tokens,
        snapshot.output_tokens,
        snapshot.session_cost_usd,
        snapshot.session_id,
        snapshot.project_dir,
    ]


#: 取り残されたロックを何秒で無効とみなすか。
_LOCK_STALE_SECONDS = 5.0


class _Lock:
    """追記と書き直しを直列化する最小のロック。

    ターミナルを複数開くとそれぞれの Claude Code が statusline を呼ぶので、書き込みが
    重なりうる。Windows では追記の原子性が保証されないため行が混ざる。

    取れなければ **諦めて書かない**。1件の履歴を落としても消費速度の算出にほぼ影響が
    無く、statusline を待たせるほうが害が大きい。
    """

    def __init__(self, attempts: int = 20, wait: float = 0.005) -> None:
        self._attempts = attempts
        self._wait = wait
        self._fd: int | None = None

    def __enter__(self) -> bool:
        for _ in range(self._attempts):
            try:
                self._fd = os.open(_lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return True
            except FileExistsError:
                self._drop_if_stale()
                time.sleep(self._wait)
            except OSError:
                return False
        return False

    def _drop_if_stale(self) -> None:
        """異常終了で取り残されたロックを片付ける。

        これが無いと、一度 statusline が強制終了した時点で以後ずっと書けなくなる。
        """
        try:
            if time.time() - _lock_path().stat().st_mtime > _LOCK_STALE_SECONDS:
                _lock_path().unlink()
        except OSError:
            pass

    def __exit__(self, *exc_info) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
            _lock_path().unlink()
        except OSError:
            pass


def _append(snapshot: Snapshot) -> None:
    path = _history_path()
    with _Lock() as acquired:
        if not acquired:
            return
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(COLUMNS)
            writer.writerow(_row(snapshot))


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
            # 生データは最新の1件だけ持つ。履歴の全行に持たせると 90日で 100MB を超える。
            # statusline の JSON に認証情報は含まれない（仕様書 §17 を確認済み）。
            body["raw"] = raw

        tmp = directory / "latest.json.tmp"
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_latest_path())

        if changed(previous, snapshot):
            _append(snapshot)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 読み出し
# --------------------------------------------------------------------------

def _from_dict(body: dict) -> Snapshot:
    def window(key: str) -> Window:
        raw = body.get(key) or {}
        return Window(used_percent=raw.get("used_percent"), resets_at=raw.get("resets_at"))

    known = {
        f for f in Snapshot.__dataclass_fields__
        if f not in ("five_hour", "seven_day", "spend_limit")
    }
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


def _from_row(row: dict) -> Snapshot | None:
    def number(key: str) -> float | None:
        value = (row.get(key) or "").strip()
        try:
            return float(value) if value else None
        except ValueError:
            return None

    def integer(key: str) -> int | None:
        value = number(key)
        return int(value) if value is not None else None

    captured = (row.get("captured_at") or "").strip()
    if not captured:
        return None
    try:
        datetime.fromisoformat(captured)
    except ValueError:
        return None

    return Snapshot(
        captured_at=captured,
        five_hour=Window(number("five_hour_used_percent"), integer("five_hour_resets_at")),
        seven_day=Window(number("seven_day_used_percent"), integer("seven_day_resets_at")),
        model=row.get("model") or None,
        context_used_percent=number("context_used_percent"),
        input_tokens=integer("input_tokens"),
        output_tokens=integer("output_tokens"),
        session_cost_usd=number("session_cost_usd"),
        session_id=row.get("session_id") or None,
        project_dir=row.get("project_dir") or None,
    )


def load_history(limit: int = 2000) -> list[Snapshot]:
    """**古い順**に返す。消費速度は時系列で見るため。

    壊れた行は読み飛ばす。複数セッションの書き込みが重なって混ざった行が残っていても、
    そこで全部を諦めない。
    """
    try:
        with _history_path().open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []
    out: list[Snapshot] = []
    for row in rows[-limit:]:
        try:
            shot = _from_row(row)
        except (TypeError, ValueError):
            continue
        if shot is not None:
            out.append(shot)
    return out


def prune(retention_days: int = 90) -> int:
    """保持期間を過ぎた履歴を捨てる（仕様書 §16）。返り値は消した行数。

    書き直しなので追記と同じロックを取る。取れなければ何もしない（次の機会に消せる）。
    """
    if retention_days <= 0:
        return 0
    path = _history_path()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return 0

    keep = []
    for row in rows:
        try:
            if datetime.fromisoformat((row.get("captured_at") or "")) >= cutoff:
                keep.append(row)
        except ValueError:
            continue     # 日付として読めない行は捨てる

    removed = len(rows) - len(keep)
    if removed <= 0:
        return 0

    with _Lock() as acquired:
        if not acquired:
            return 0
        tmp = path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(keep)
        tmp.replace(path)
    return removed
