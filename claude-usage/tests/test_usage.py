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
    snap.save(shot(five=(10.0, NOW + timedelta(hours=3))))
    path = snap.data_dir() / "snapshots.jsonl"
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
