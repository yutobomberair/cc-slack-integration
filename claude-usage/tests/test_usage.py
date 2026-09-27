"""claude-usage のテスト。

重点は**ペース判定**。この道具の目的は「上限に当てない・余らせない」なので、
使いすぎと使い残しの両方を正しく言い当てられるかが本体。

次に**壊れない入力への耐性**。statusline は Claude Code から呼ばれるので、例外を
投げると利用者の画面が壊れる。フィールドが欠けても型が違っても落ちないこと。
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_usage import cli, display, pace  # noqa: E402
from claude_usage import snapshot as snap  # noqa: E402
from claude_usage.snapshot import Snapshot, Window  # noqa: E402

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def shot(
    *,
    five: tuple[float, datetime] | None = None,
    seven: tuple[float, datetime] | None = None,
    at: datetime = NOW,
    **extra,
) -> Snapshot:
    return Snapshot(
        captured_at=at.isoformat(timespec="seconds"),
        five_hour=Window(five[0], epoch(five[1])) if five else Window(),
        seven_day=Window(seven[0], epoch(seven[1])) if seven else Window(),
        **extra,
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """本物の ~/.claude-usage を触らない。"""
    monkeypatch.setenv("CLAUDE_USAGE_HOME", str(tmp_path / "home"))


# --------------------------------------------------------------------------
# statusline JSON の読み取り
# --------------------------------------------------------------------------

def test_parses_official_rate_limits():
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 78, "resets_at": 1790496600},
            "seven_day": {"used_percentage": 49, "resets_at": 1791032400},
        }
    }
    result = snap.parse(payload, now=NOW)
    assert result.five_hour.used_percent == 78
    assert result.five_hour.resets_at == 1790496600
    assert result.seven_day.used_percent == 49
    assert result.has_official_limits


def test_missing_rate_limits_is_not_an_error():
    # 認証方式や版によっては入ってこない。0% と混同しないこと
    result = snap.parse({"model": {"display_name": "Opus 5"}}, now=NOW)
    assert result.five_hour.used_percent is None
    assert result.five_hour.available is False
    assert result.has_official_limits is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rate_limits": "壊れている"},
        {"rate_limits": {"five_hour": "壊れている"}},
        {"rate_limits": {"five_hour": {"used_percentage": "78"}}},   # 文字列は捨てる
        {"rate_limits": {"five_hour": {"used_percentage": True}}},   # bool も捨てる
        {"context_window": None, "cost": [], "model": 3},
    ],
)
def test_broken_payloads_do_not_raise(payload):
    result = snap.parse(payload, now=NOW)
    assert result.five_hour.used_percent is None


def test_parses_session_fields():
    payload = {
        "model": {"display_name": "Opus 5"},
        "context_window": {"used_percentage": 62.4, "total_input_tokens": 124532,
                           "total_output_tokens": 31204, "context_window_size": 200000},
        "cost": {"total_cost_usd": 1.23},
        "session_id": "abc",
        "workspace": {"project_dir": "C:/x"},
        "version": "2.1.283",
    }
    result = snap.parse(payload, now=NOW)
    assert result.model == "Opus 5"
    assert result.context_used_percent == 62.4
    assert result.input_tokens == 124532
    assert result.session_cost_usd == 1.23
    assert result.project_dir == "C:/x"


# --------------------------------------------------------------------------
# 枠の計算
# --------------------------------------------------------------------------

def test_remaining_is_the_complement():
    assert Window(78.0).remaining_percent == 22.0


def test_remaining_never_goes_negative():
    # spend_limit は 100 を超えうる（ドキュメント記載）
    assert Window(120.0).remaining_percent == 0.0


def test_seconds_until_reset():
    window = Window(10.0, epoch(NOW + timedelta(hours=2)))
    assert window.seconds_until_reset(NOW) == pytest.approx(7200)


def test_past_reset_is_clamped_to_zero():
    window = Window(10.0, epoch(NOW - timedelta(hours=1)))
    assert window.seconds_until_reset(NOW) == 0.0


# --------------------------------------------------------------------------
# 保存（statusline は高頻度で呼ばれる）
# --------------------------------------------------------------------------

def test_unchanged_snapshots_are_not_appended():
    reset = NOW + timedelta(hours=3)
    first = shot(five=(10.0, reset))
    snap.save(first)
    snap.save(shot(five=(10.0, reset), at=NOW + timedelta(seconds=1)))

    assert len(snap.load_history()) == 1     # 利用率が動いていないので増やさない


def test_changed_usage_is_appended():
    reset = NOW + timedelta(hours=3)
    snap.save(shot(five=(10.0, reset)))
    snap.save(shot(five=(12.0, reset), at=NOW + timedelta(minutes=10)))

    assert len(snap.load_history()) == 2


def test_latest_is_always_overwritten():
    reset = NOW + timedelta(hours=3)
    snap.save(shot(five=(10.0, reset)))
    snap.save(shot(five=(10.0, reset), at=NOW + timedelta(seconds=5), model="Sonnet 5"))

    assert snap.load_latest().model == "Sonnet 5"


def test_roundtrip_preserves_windows():
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3)), seven=(5.0, NOW + timedelta(days=6))))
    loaded = snap.load_latest()
    assert loaded.five_hour.used_percent == 10.0
    assert loaded.seven_day.used_percent == 5.0


def test_no_snapshot_yet():
    assert snap.load_latest() is None
    assert snap.load_history() == []


def test_corrupt_history_lines_are_skipped():
    # 複数セッションの書き込みが重なって混ざった行が残っても、全部を諦めない
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    path = snap.data_dir() / "snapshots.csv"
    path.write_text(path.read_text(encoding="utf-8") + "壊れた行\n", encoding="utf-8")
    assert len(snap.load_history()) == 1


# --------------------------------------------------------------------------
# 消費速度（枠のリセットを跨がないこと）
# --------------------------------------------------------------------------

def test_burn_needs_two_points():
    history = [shot(five=(10.0, NOW + timedelta(hours=3)))]
    assert pace.burn(history, "five_hour", NOW).measured is False


def test_burn_rate_from_two_points():
    reset = NOW + timedelta(hours=3)
    history = [
        shot(five=(10.0, reset), at=NOW - timedelta(hours=2)),
        shot(five=(30.0, reset), at=NOW),
    ]
    # 2時間で 20% → 10%/時
    assert pace.burn(history, "five_hour", NOW).percent_per_hour == pytest.approx(10.0)


def test_burn_ignores_spans_that_are_too_short():
    reset = NOW + timedelta(hours=3)
    history = [
        shot(five=(10.0, reset), at=NOW - timedelta(seconds=30)),
        shot(five=(30.0, reset), at=NOW),
    ]
    # 30秒で 20% を「40%/時」と言い出すと見通しが暴れる
    assert pace.burn(history, "five_hour", NOW).measured is False


def test_history_is_cut_at_a_window_reset():
    old_reset = NOW - timedelta(minutes=30)
    new_reset = NOW + timedelta(hours=4, minutes=30)
    history = [
        shot(five=(90.0, old_reset), at=NOW - timedelta(hours=2)),   # 前の枠
        shot(five=(5.0, new_reset), at=NOW - timedelta(hours=1)),    # リセット後
        shot(five=(15.0, new_reset), at=NOW),
    ]
    kept = pace.current_window_history(history, "five_hour")
    assert len(kept) == 2                                            # 前の枠は捨てる
    assert pace.burn(history, "five_hour", NOW).percent_per_hour == pytest.approx(10.0)


def test_usage_going_down_is_treated_as_a_reset():
    # resets_at の更新が遅れても、利用率が下がれば別の枠と見る
    reset = NOW + timedelta(hours=3)
    history = [
        shot(five=(90.0, reset), at=NOW - timedelta(hours=2)),
        shot(five=(10.0, reset), at=NOW - timedelta(hours=1)),
        shot(five=(20.0, reset), at=NOW),
    ]
    assert len(pace.current_window_history(history, "five_hour")) == 2


def test_exhaustion_time_is_projected():
    reset = NOW + timedelta(hours=3)
    history = [
        shot(five=(50.0, reset), at=NOW - timedelta(hours=1)),
        shot(five=(70.0, reset), at=NOW),
    ]
    # 20%/時 で残り 30% → 1.5時間後
    result = pace.burn(history, "five_hour", NOW)
    assert result.exhausts_at == NOW + timedelta(hours=1.5)


# --------------------------------------------------------------------------
# 5時間枠の判定（リセット前に止められないか）
# --------------------------------------------------------------------------

def test_five_hour_warns_when_it_will_run_out_before_reset():
    reset = NOW + timedelta(hours=3)
    current = shot(five=(70.0, reset))
    history = [shot(five=(50.0, reset), at=NOW - timedelta(hours=1)), current]

    verdict = pace.five_hour_verdict(current, history, NOW)
    assert verdict.level == "warning"
    assert "使い切ります" in verdict.headline


def test_five_hour_is_ok_when_it_lasts_past_reset():
    reset = NOW + timedelta(hours=3)
    current = shot(five=(20.0, reset))
    history = [shot(five=(18.0, reset), at=NOW - timedelta(hours=1)), current]

    assert pace.five_hour_verdict(current, history, NOW).level == "ok"


def test_five_hour_warns_when_nearly_empty_even_without_history():
    current = shot(five=(95.0, NOW + timedelta(hours=1)))
    verdict = pace.five_hour_verdict(current, [current], NOW)
    assert verdict.level == "warning"


def test_five_hour_critical_when_exhausted():
    current = shot(five=(100.0, NOW + timedelta(hours=1)))
    verdict = pace.five_hour_verdict(current, [current], NOW)
    assert verdict.level == "critical"
    assert "待つ" in verdict.headline


def test_five_hour_unknown_without_official_value():
    current = shot()
    assert pace.five_hour_verdict(current, [current], NOW).level == "unknown"


# --------------------------------------------------------------------------
# 7日枠の判定（使いすぎと使い残しの両方）
# --------------------------------------------------------------------------

def test_seven_day_flags_underuse():
    """目的の半分はこれ。余らせるのもよくない。"""
    reset = NOW + timedelta(days=5)
    current = shot(seven=(10.0, reset))
    # 残り 90% を5日で使える（18%/日）のに、実績 2.4%/日
    history = [shot(seven=(9.0, reset), at=NOW - timedelta(hours=10)), current]

    verdict = pace.seven_day_verdict(current, history, NOW)
    assert verdict.level == "underuse"
    assert "使い残し" in verdict.headline


def test_seven_day_flags_overuse():
    reset = NOW + timedelta(days=5)
    current = shot(seven=(50.0, reset))
    # 残り 50% を5日で使える（10%/日）のに、実績 48%/日
    history = [shot(seven=(30.0, reset), at=NOW - timedelta(hours=10)), current]

    verdict = pace.seven_day_verdict(current, history, NOW)
    assert verdict.level == "warning"
    assert "速すぎ" in verdict.headline


def test_seven_day_ok_when_on_pace():
    reset = NOW + timedelta(days=5)
    current = shot(seven=(50.0, reset))
    # 残り 50% ÷ 5日 = 10%/日。実績も約 10%/日
    history = [shot(seven=(45.8, reset), at=NOW - timedelta(hours=10)), current]

    assert pace.seven_day_verdict(current, history, NOW).level == "ok"


def test_seven_day_shows_the_daily_budget_without_history():
    reset = NOW + timedelta(days=5)
    current = shot(seven=(50.0, reset))
    verdict = pace.seven_day_verdict(current, [current], NOW)
    assert verdict.level == "ok"
    assert "10.0%" in verdict.detail      # 残り 50% ÷ 5日


def test_seven_day_critical_when_exhausted():
    current = shot(seven=(100.0, NOW + timedelta(days=2)))
    assert pace.seven_day_verdict(current, [current], NOW).level == "critical"


# --------------------------------------------------------------------------
# まとめ
# --------------------------------------------------------------------------

def test_overall_prefers_the_heaviest_level():
    assert pace.overall([
        pace.Verdict("ok", "a"), pace.Verdict("warning", "b"), pace.Verdict("underuse", "c"),
    ]).level == "warning"


def test_overall_reports_underuse_when_nothing_worse():
    verdicts = [pace.Verdict("ok", "a"), pace.Verdict("underuse", "b")]
    assert pace.overall(verdicts).level == "underuse"


def test_overall_unknown_when_nothing_is_known():
    assert pace.overall([pace.Verdict("unknown", "a")]).level == "unknown"


# --------------------------------------------------------------------------
# 文字コードへの追従（Windows の cp932 で実際に空表示になった）
# --------------------------------------------------------------------------

def test_bar_falls_back_to_ascii(monkeypatch):
    monkeypatch.setattr(display.sys, "stdout", type("S", (), {"encoding": "cp932"})())
    assert display.bar(50, width=10) == "#####-----"


def test_bar_uses_blocks_when_possible(monkeypatch):
    monkeypatch.setattr(display.sys, "stdout", type("S", (), {"encoding": "utf-8"})())
    assert display.bar(50, width=10) == "\u2588" * 5 + "\u2591" * 5


def test_bar_distinguishes_unknown_from_zero(monkeypatch):
    monkeypatch.setattr(display.sys, "stdout", type("S", (), {"encoding": "cp932"})())
    assert display.bar(None, width=4) == "????"
    assert display.bar(0, width=4) == "----"


def test_pick_falls_back(monkeypatch):
    monkeypatch.setattr(display.sys, "stdout", type("S", (), {"encoding": "cp932"})())
    assert display.pick("\u21ba", "reset") == "reset"


# --------------------------------------------------------------------------
# JSON 出力（仕様書 §10, §20）
# --------------------------------------------------------------------------

def test_json_marks_official_values():
    current = shot(five=(78.0, NOW + timedelta(hours=1)), seven=(49.0, NOW + timedelta(days=4)))
    body = cli.build_json(current, [current], NOW)
    assert body["subscription"]["five_hour"]["confidence"] == "official"
    assert body["subscription"]["five_hour"]["used_percent"] == 78.0


def test_json_does_not_invent_a_daily_value():
    """仕様書 §20。取れない値を推測して出さない。"""
    current = shot(five=(78.0, NOW + timedelta(hours=1)))
    daily = cli.build_json(current, [current], NOW)["subscription"]["daily"]
    assert daily["used_percent"] is None
    assert daily["confidence"] == "unavailable"


def test_json_marks_unavailable_windows():
    current = shot()
    body = cli.build_json(current, [current], NOW)
    assert body["subscription"]["five_hour"]["confidence"] == "unavailable"
    assert body["subscription"]["five_hour"]["used_percent"] is None


def test_json_flags_stale_snapshots():
    old = shot(five=(10.0, NOW + timedelta(hours=1)), at=NOW - timedelta(hours=5))
    assert cli.build_json(old, [old], NOW)["stale"] is True


def test_json_is_serialisable():
    current = shot(
        five=(78.0, NOW + timedelta(hours=1)), seven=(49.0, NOW + timedelta(days=4))
    )
    json.dumps(cli.build_json(current, [current], NOW))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_explains_how_to_set_up_when_empty(capsys):
    assert cli.main([]) == 1
    assert "statusLine" in capsys.readouterr().out


def test_cli_returns_2_when_pace_is_a_problem(capsys):
    reset = NOW + timedelta(hours=3)
    snap.save(shot(five=(50.0, reset), at=NOW - timedelta(hours=1)))
    snap.save(shot(five=(95.0, reset), at=NOW))
    assert cli.main([]) == 2         # 監視スクリプトから使えるように


def test_cli_status_runs(capsys):
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    cli.main(["status"])
    assert "5h Window" in capsys.readouterr().out


def test_cli_json_output(capsys):
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    cli.main(["--json"])
    body = json.loads(capsys.readouterr().out)
    assert body["subscription"]["five_hour"]["used_percent"] == 10.0


def test_summary_shows_the_update_time(capsys):
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    cli.main([])
    # 取りに行けない値なので、いつの値かを必ず出す
    assert "Updated:" in capsys.readouterr().out


# --------------------------------------------------------------------------
# CSV での保存（SQLite は使わない。この規模では SQL の利点が出ない）
# --------------------------------------------------------------------------

def test_history_is_csv_with_a_fixed_header():
    """CSV にしたのは Excel でそのまま開けるから。列順は固定。"""
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    lines = (snap.data_dir() / "snapshots.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == snap.COLUMNS


def test_history_rows_stay_small():
    """raw を履歴に入れていた頃は1行 2.5KB で、90日 107MB になる見込みだった。"""
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3)), model="Opus 5"))
    rows = (snap.data_dir() / "snapshots.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows[1].encode("utf-8")) < 200


def test_raw_is_kept_only_in_latest():
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))), raw={"rate_limits": {"x": 1}})
    body = json.loads((snap.data_dir() / "latest.json").read_text(encoding="utf-8"))
    assert "raw" in body
    history_text = (snap.data_dir() / "snapshots.csv").read_text(encoding="utf-8")
    assert "rate_limits" not in history_text


def test_prune_drops_rows_past_retention():
    reset = NOW + timedelta(hours=3)
    snap.save(shot(five=(10.0, reset), at=NOW - timedelta(days=120)))
    snap.save(shot(five=(11.0, reset), at=NOW - timedelta(days=1)))

    assert snap.prune(retention_days=90) == 1
    remaining = snap.load_history()
    assert len(remaining) == 1
    assert remaining[0].five_hour.used_percent == 11.0


def test_prune_keeps_everything_within_retention():
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    assert snap.prune(retention_days=90) == 0
    assert len(snap.load_history()) == 1


def test_prune_is_a_noop_without_history():
    assert snap.prune(retention_days=90) == 0


def test_prune_disabled_by_zero():
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3)), at=NOW - timedelta(days=500)))
    assert snap.prune(retention_days=0) == 0
    assert len(snap.load_history()) == 1


def test_stale_lock_does_not_block_writes_forever():
    """statusline が強制終了してロックが残ると、以後ずっと書けなくなる。"""
    import os as _os
    import time as _time

    snap.data_dir().mkdir(parents=True, exist_ok=True)
    lock = snap.data_dir() / "history.lock"
    lock.write_text("", encoding="utf-8")
    old = _time.time() - snap._LOCK_STALE_SECONDS - 1
    _os.utime(lock, (old, old))

    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    assert len(snap.load_history()) == 1


def test_held_lock_skips_the_history_but_keeps_latest():
    """ロックが取れないときは履歴を諦める。statusline を待たせる害のほうが大きい。"""
    snap.data_dir().mkdir(parents=True, exist_ok=True)
    (snap.data_dir() / "history.lock").write_text("", encoding="utf-8")

    snap.save(shot(five=(10.0, NOW + timedelta(hours=3)), model="Opus 5"))
    assert snap.load_latest().model == "Opus 5"       # 最新値は残る
    assert snap.load_history() == []                  # 履歴は落ちる


# --------------------------------------------------------------------------
# アラート（仕様書 §11）
#
# statusline は常に呼ばれているので、素朴に閾値を見ると毎回鳴る。
# 「同じことを鳴らし続けない」が本体なので、そこを厚く見る。
# --------------------------------------------------------------------------

from claude_usage import alerts as alerts_mod  # noqa: E402
from claude_usage import config as config_mod  # noqa: E402
from claude_usage import notifiers  # noqa: E402

DEFAULTS = config_mod.Config()


@pytest.mark.parametrize(
    "percent,level",
    [(0, "none"), (69, "none"), (70, "warning"), (84, "warning"),
     (85, "high"), (94, "high"), (95, "critical"), (100, "critical"), (None, "none")],
)
def test_threshold_levels(percent, level):
    assert alerts_mod.threshold_level(percent, config_mod.Thresholds()) == level


def test_no_alert_below_the_threshold():
    current = shot(five=(50.0, NOW + timedelta(hours=3)))
    assert alerts_mod.decide(current, [current], DEFAULTS, NOW) == []


def test_threshold_alert_fires():
    current = shot(five=(72.0, NOW + timedelta(hours=3)))
    found = alerts_mod.decide(current, [current], DEFAULTS, NOW)
    assert [a.level for a in found] == ["warning"]
    assert "72%" in found[0].headline
    assert "リセット" in found[0].detail       # いつ復活するかが要る


def test_alerts_can_be_disabled():
    current = shot(five=(99.0, NOW + timedelta(hours=3)))
    disabled = config_mod.Config(alerts_enabled=False)
    assert alerts_mod.decide(current, [current], disabled, NOW) == []


def test_underuse_is_reported():
    """目的の半分。余らせるのもよくない。"""
    reset = NOW + timedelta(days=5)
    current = shot(seven=(10.0, reset))
    history = [shot(seven=(9.0, reset), at=NOW - timedelta(hours=10)), current]

    found = alerts_mod.decide(current, history, DEFAULTS, NOW)
    assert [a.level for a in found] == ["underuse"]


def test_underuse_can_be_turned_off():
    reset = NOW + timedelta(days=5)
    current = shot(seven=(10.0, reset))
    history = [shot(seven=(9.0, reset), at=NOW - timedelta(hours=10)), current]
    quiet = config_mod.Config(thresholds=config_mod.Thresholds(underuse=False))

    assert alerts_mod.decide(current, history, quiet, NOW) == []


def test_one_alert_per_window_keeps_the_heavier():
    """70%到達とペース警告を2通出すと読まれなくなる。"""
    reset = NOW + timedelta(hours=3)
    current = shot(five=(72.0, reset))
    # 20%/時 で残り 28% → リセット前に尽きる（ペース側も warning）
    history = [shot(five=(52.0, reset), at=NOW - timedelta(hours=1)), current]

    found = alerts_mod.decide(current, history, DEFAULTS, NOW)
    assert len(found) == 1


def test_both_windows_can_alert():
    five_reset = NOW + timedelta(hours=3)
    seven_reset = NOW + timedelta(days=5)
    current = shot(five=(72.0, five_reset), seven=(90.0, seven_reset))
    found = alerts_mod.decide(current, [current], DEFAULTS, NOW)
    assert {a.window for a in found} == {"five_hour", "seven_day"}


# --- 重複の抑制 ---

def test_the_same_level_does_not_fire_twice():
    reset = NOW + timedelta(hours=3)
    current = shot(five=(72.0, reset))
    found = alerts_mod.decide(current, [current], DEFAULTS, NOW)

    first, state = alerts_mod.filter_new(current, found, {})
    assert len(first) == 1
    second, _ = alerts_mod.filter_new(current, found, state)
    assert second == []                     # statusline は何度も呼ばれる


def test_rising_to_a_heavier_level_fires_again():
    reset = NOW + timedelta(hours=3)
    warned = shot(five=(72.0, reset))
    _, state = alerts_mod.filter_new(
        warned, alerts_mod.decide(warned, [warned], DEFAULTS, NOW), {}
    )

    worse = shot(five=(96.0, reset))
    fresh, _ = alerts_mod.filter_new(
        worse, alerts_mod.decide(worse, [worse], DEFAULTS, NOW), state
    )
    assert [a.level for a in fresh] == ["critical"]


def test_dropping_back_does_not_fire():
    reset = NOW + timedelta(hours=3)
    worse = shot(five=(96.0, reset))
    _, state = alerts_mod.filter_new(
        worse, alerts_mod.decide(worse, [worse], DEFAULTS, NOW), {}
    )

    milder = shot(five=(72.0, reset))
    fresh, _ = alerts_mod.filter_new(
        milder, alerts_mod.decide(milder, [milder], DEFAULTS, NOW), state
    )
    assert fresh == []


def test_a_new_window_fires_again():
    """枠がリセットされたら鳴らし直す。でないと次の枠で警告が出ない。"""
    old = shot(five=(72.0, NOW + timedelta(hours=1)))
    _, state = alerts_mod.filter_new(
        old, alerts_mod.decide(old, [old], DEFAULTS, NOW), {}
    )

    fresh_window = shot(five=(72.0, NOW + timedelta(hours=6)))   # resets_at が変わった
    fresh, _ = alerts_mod.filter_new(
        fresh_window, alerts_mod.decide(fresh_window, [fresh_window], DEFAULTS, NOW), state
    )
    assert [a.level for a in fresh] == ["warning"]


def test_underuse_is_not_repeated_within_a_window():
    reset = NOW + timedelta(days=5)
    current = shot(seven=(10.0, reset))
    history = [shot(seven=(9.0, reset), at=NOW - timedelta(hours=10)), current]
    found = alerts_mod.decide(current, history, DEFAULTS, NOW)

    first, state = alerts_mod.filter_new(current, found, {})
    assert len(first) == 1
    again, _ = alerts_mod.filter_new(current, found, state)
    assert again == []


def test_state_survives_a_restart():
    reset = NOW + timedelta(hours=3)
    current = shot(five=(72.0, reset))
    _, state = alerts_mod.filter_new(
        current, alerts_mod.decide(current, [current], DEFAULTS, NOW), {}
    )
    alerts_mod.save_state(state)

    reloaded = alerts_mod.load_state()
    fresh, _ = alerts_mod.filter_new(
        current, alerts_mod.decide(current, [current], DEFAULTS, NOW), reloaded
    )
    assert fresh == []


def test_corrupt_state_is_ignored():
    alerts_mod.data_dir().mkdir(parents=True, exist_ok=True)
    alerts_mod.state_path().write_text("{ broken", encoding="utf-8")
    assert alerts_mod.load_state() == {}


# --- 通知先 ---

def test_file_notifier_keeps_a_record():
    alert = alerts_mod.Alert("five_hour", "warning", "見出し", "詳細")
    assert notifiers.notify_file(alert) is True
    body = notifiers.log_path().read_text(encoding="utf-8")
    assert "warning" in body and "見出し" in body


def test_deliver_only_uses_enabled_notifiers():
    alert = alerts_mod.Alert("five_hour", "warning", "見出し")
    quiet = config_mod.Config(
        notifiers=config_mod.Notifiers(terminal=False, file=True, windows=False, slack=False)
    )
    result = notifiers.deliver([alert], quiet)
    assert result == {"terminal": 0, "file": 1, "windows": 0, "slack": 0}


def test_slack_needs_a_channel_and_token():
    alert = alerts_mod.Alert("five_hour", "warning", "見出し")
    assert notifiers.notify_slack(alert, config_mod.Notifiers()) is False


def test_slack_posts_when_configured(monkeypatch):
    captured = {}

    class Response:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["auth"] = request.headers["Authorization"]
        return Response()

    monkeypatch.setattr(notifiers.urllib.request, "urlopen", fake_urlopen)
    settings = config_mod.Notifiers(slack=True, slack_channel="C1", slack_token="xoxb-x")
    alert = alerts_mod.Alert("five_hour", "critical", "枠を使い切りました", "リセット 17:10")

    assert notifiers.notify_slack(alert, settings) is True
    assert captured["body"]["channel"] == "C1"
    assert "枠を使い切りました" in captured["body"]["text"]
    # 本文にトークンを混ぜない（仕様書 §17）
    assert "xoxb-" not in captured["body"]["text"]


def test_slack_failure_is_not_fatal(monkeypatch):
    def boom(request, timeout=None):
        raise notifiers.urllib.error.URLError("down")

    monkeypatch.setattr(notifiers.urllib.request, "urlopen", boom)
    settings = config_mod.Notifiers(slack=True, slack_channel="C1", slack_token="xoxb-x")
    alert = alerts_mod.Alert("five_hour", "warning", "x")
    assert notifiers.notify_slack(alert, settings) is False


def test_one_broken_notifier_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(notifiers, "notify_slack", lambda a, s: False)
    settings = config_mod.Config(
        notifiers=config_mod.Notifiers(terminal=False, file=True, slack=True,
                                       slack_channel="C1", slack_token="x")
    )
    result = notifiers.deliver([alerts_mod.Alert("five_hour", "warning", "x")], settings)
    assert result["file"] == 1 and result["slack"] == 0


def test_powershell_quoting_escapes_single_quotes():
    # 通知本文にアポストロフィが入るとコマンドが壊れる
    quoted = notifiers._ps_quote("it" + chr(39) + "s")
    assert quoted == chr(39) + "it" + chr(39) * 2 + "s" + chr(39)


# --- 設定 ---

def test_config_defaults_without_a_file():
    settings = config_mod.load()
    assert settings.thresholds.warning == 70.0
    assert settings.retention_days == 90
    assert settings.alerts_enabled is True


def test_config_is_read():
    config_mod.config_path().parent.mkdir(parents=True, exist_ok=True)
    config_mod.config_path().write_text(
        json.dumps({"thresholds": {"warning": 50}, "retention_days": 30,
                    "notifiers": {"slack": True, "slack_channel": "C9"}}),
        encoding="utf-8",
    )
    settings = config_mod.load()
    assert settings.thresholds.warning == 50.0
    assert settings.thresholds.high == 85.0        # 書いていない項目は既定のまま
    assert settings.retention_days == 30
    assert settings.notifiers.slack_channel == "C9"


@pytest.mark.parametrize(
    "body", ["{ broken", "[]", '{"thresholds": "壊れている"}', '{"retention_days": "90"}']
)
def test_broken_config_falls_back_to_defaults(body):
    config_mod.config_path().parent.mkdir(parents=True, exist_ok=True)
    config_mod.config_path().write_text(body, encoding="utf-8")
    # 設定の不備で statusline を落とさない
    assert config_mod.load().thresholds.warning == 70.0


def test_config_template_does_not_overwrite():
    assert config_mod.write_template() is True
    config_mod.config_path().write_text('{"retention_days": 7}', encoding="utf-8")
    assert config_mod.write_template() is False
    assert config_mod.load().retention_days == 7


# --- CLI ---

def test_cli_alerts_view(capsys):
    snap.save(shot(five=(72.0, NOW + timedelta(hours=3))))
    assert cli.main(["alerts"]) == 0
    out = capsys.readouterr().out
    assert "いまの判定" in out
    assert "warning" in out


def test_cli_config_writes_a_template(capsys):
    assert cli.main(["config"]) == 0
    assert config_mod.config_path().exists()


def test_cli_prune(capsys):
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3)), at=NOW - timedelta(days=200)))
    assert cli.main(["prune"]) == 0
    assert "削除しました" in capsys.readouterr().out


# --------------------------------------------------------------------------
# プロジェクト別集計（仕様書 §7）
#
# 最重要は「Quota の配分ではない」ことを守れているか。Subscription Quota は
# プロジェクト単位で提供されないので、トークン数の比率としてしか出せない。
# --------------------------------------------------------------------------

from claude_usage import transcript  # noqa: E402


def write_transcript(root: Path, name: str, records: list[dict]) -> Path:
    """transcript を1本作る。``~/.claude/projects/<dir>/<session>.jsonl`` の形。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "session.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def assistant(cwd: str, *, out: int = 100, cache_new: int = 0, cache_read: int = 0,
              inp: int = 1, at: datetime = NOW, model: str = "claude-opus-5") -> dict:
    return {
        "type": "assistant",
        "cwd": cwd,
        "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_new,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


def test_aggregates_tokens_per_cwd(tmp_path):
    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant("C:/work/alpha", out=300)])
    write_transcript(root, "b", [assistant("C:/work/beta", out=100)])

    result = transcript.aggregate(root=root, by_repo=False)
    assert result.by_path["C:/work/alpha"].output == 300
    assert result.total.output == 400
    assert result.total.messages == 2


def test_share_excludes_cache_reads(tmp_path):
    """cache_read は桁が違う（実測 45億 vs 1,860万）。含めると比率が
    セッションの長さに支配されて作業量を表さなくなる。"""
    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant("C:/work/alpha", out=100, cache_read=1_000_000)])
    write_transcript(root, "b", [assistant("C:/work/beta", out=100)])

    rows = transcript.ranked(transcript.aggregate(root=root, by_repo=False))
    shares = {path: share for path, _, share in rows}
    assert shares["C:/work/alpha"] == pytest.approx(50.0, abs=0.5)
    assert shares["C:/work/beta"] == pytest.approx(50.0, abs=0.5)


def test_cache_reads_are_still_reported(tmp_path):
    # 比率からは外すが、内訳としては見せる
    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant("C:/work/alpha", cache_read=1234)])
    result = transcript.aggregate(root=root, by_repo=False)
    assert result.by_path["C:/work/alpha"].cache_read == 1234


def test_only_assistant_records_are_counted(tmp_path):
    root = tmp_path / "projects"
    write_transcript(root, "a", [
        assistant("C:/work/alpha", out=100),
        {"type": "user", "cwd": "C:/work/alpha", "message": {"usage": {"output_tokens": 999}}},
    ])
    assert transcript.aggregate(root=root, by_repo=False).total.output == 100


def test_broken_lines_are_skipped(tmp_path):
    root = tmp_path / "projects"
    path = write_transcript(root, "a", [assistant("C:/work/alpha", out=100)])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"usage": broken\n')
        handle.write('"usage" not json at all\n')
    assert transcript.aggregate(root=root, by_repo=False).total.output == 100


def test_records_without_usage_are_ignored(tmp_path):
    root = tmp_path / "projects"
    write_transcript(root, "a", [
        assistant("C:/work/alpha", out=100),
        {"type": "assistant", "cwd": "C:/work/alpha", "message": {"model": "x"}},
    ])
    assert transcript.aggregate(root=root, by_repo=False).total.messages == 1


def test_missing_projects_dir_is_not_an_error(tmp_path):
    result = transcript.aggregate(root=tmp_path / "nope")
    assert result.total.messages == 0
    assert result.by_path == {}


# --- 期間の絞り込み ---

def test_since_excludes_older_records(tmp_path):
    root = tmp_path / "projects"
    write_transcript(root, "a", [
        assistant("C:/work/alpha", out=100, at=NOW - timedelta(days=30)),
        assistant("C:/work/alpha", out=200, at=NOW - timedelta(hours=1)),
    ])
    result = transcript.aggregate(since=NOW - timedelta(days=7), root=root, by_repo=False)
    assert result.total.output == 200
    assert result.skipped_outside_range == 1


def test_records_without_a_timestamp_are_excluded_when_filtering(tmp_path):
    root = tmp_path / "projects"
    record = assistant("C:/work/alpha", out=100)
    del record["timestamp"]
    write_transcript(root, "a", [record])
    result = transcript.aggregate(since=NOW - timedelta(days=7), root=root, by_repo=False)
    assert result.total.output == 0


def test_window_start_follows_the_actual_quota_window():
    """暦の7日ではなく実際の枠に合わせる。「この枠でどこが重かったか」が見たい情報。"""
    reset = NOW + timedelta(days=2)
    start = transcript.window_start(int(reset.timestamp()))
    assert start == pytest.approx(reset - timedelta(days=7), abs=timedelta(seconds=1))


def test_window_start_without_a_reset():
    assert transcript.window_start(None) is None


# --- リポジトリ単位のまとめ（cd: で階層が分かれるため） ---

def test_subdirectories_roll_up_to_the_repo_root(tmp_path):
    repo = tmp_path / "propose"
    (repo / ".git").mkdir(parents=True)
    (repo / "movie").mkdir()

    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant(str(repo), out=100)])
    write_transcript(root, "b", [assistant(str(repo / "movie"), out=300)])

    result = transcript.aggregate(root=root, by_repo=True)
    assert result.by_path[str(repo)].output == 400      # cd: で移った分もまとまる
    assert len(result.by_path) == 1


def test_by_cwd_keeps_the_levels_apart(tmp_path):
    repo = tmp_path / "propose"
    (repo / ".git").mkdir(parents=True)
    (repo / "movie").mkdir()

    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant(str(repo), out=100)])
    write_transcript(root, "b", [assistant(str(repo / "movie"), out=300)])

    assert len(transcript.aggregate(root=root, by_repo=False).by_path) == 2


def test_paths_outside_a_repo_stay_as_they_are(tmp_path):
    plain = tmp_path / "loose"
    plain.mkdir()
    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant(str(plain), out=100)])
    assert str(plain) in transcript.aggregate(root=root, by_repo=True).by_path


def test_missing_paths_do_not_break_rollup(tmp_path):
    # 過去の記録に残っているだけで、もう存在しないパスがある
    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant("C:/gone/forever", out=100)])
    assert "C:/gone/forever" in transcript.aggregate(root=root, by_repo=True).by_path


# --- モデル別 ---

def test_aggregates_by_model(tmp_path):
    root = tmp_path / "projects"
    write_transcript(root, "a", [
        assistant("C:/work/alpha", out=300, model="claude-opus-5"),
        assistant("C:/work/alpha", out=100, model="claude-haiku-4-5"),
    ])
    rows = transcript.ranked_models(transcript.aggregate(root=root, by_repo=False))
    assert [name for name, _, _ in rows] == ["claude-opus-5", "claude-haiku-4-5"]
    assert rows[0][2] == pytest.approx(75.0, abs=1.0)


# --- 表示 ---

@pytest.mark.parametrize(
    "count,text", [(999, "999"), (1500, "1.5k"), (2_500_000, "2.5M"), (3_000_000_000, "3.0B")]
)
def test_human_readable_token_counts(count, text):
    assert transcript.human(count) == text


def test_label_shortens_paths():
    assert transcript.label_for("C:/Users/x/work/navigation-core") == "navigation-core"


def test_label_keeps_the_parent_for_dot_directories():
    # 「.claude」だけでは、どのプロジェクトのものか分からない
    assert transcript.label_for("C:/work/propose/.claude") == "propose/.claude"


def test_cli_projects_states_it_is_not_a_quota_split(capsys, monkeypatch, tmp_path):
    """仕様書 §7 と §20。Quota の配分と誤解させない。"""
    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant("C:/work/alpha", out=100)])
    monkeypatch.setattr(transcript, "projects_dir", lambda: root)

    snap.save(shot(seven=(20.0, NOW + timedelta(days=3))))
    assert cli.main(["projects"]) == 0
    out = capsys.readouterr().out
    assert "トークン数の比率" in out
    assert "利用枠の配分ではな" in out
    assert "cache読み直しは除外" in out


def test_cli_projects_accepts_days(capsys, monkeypatch, tmp_path):
    root = tmp_path / "projects"
    write_transcript(root, "a", [assistant("C:/work/alpha", out=100)])
    monkeypatch.setattr(transcript, "projects_dir", lambda: root)

    snap.save(shot(five=(10.0, NOW + timedelta(hours=2))))
    assert cli.main(["projects", "--days", "30"]) == 0
    assert "直近 30 日" in capsys.readouterr().out
