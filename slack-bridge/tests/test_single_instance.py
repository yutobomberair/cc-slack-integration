import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.claude_runner import SESSION_EXISTS_MARKER, SESSION_MISSING_MARKER  # noqa: E402
from app.session_store import SessionStore  # noqa: E402
from app.single_instance import AlreadyRunningError, InstanceLock  # noqa: E402


# --------------------------------------------------------------------------
# 多重起動の防止
# --------------------------------------------------------------------------

def test_lock_is_acquired_and_written(tmp_path):
    path = tmp_path / "bridge.pid"
    lock = InstanceLock(path)
    lock.acquire()
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    lock.release()
    assert not path.exists()


def test_second_instance_is_refused(tmp_path):
    path = tmp_path / "bridge.pid"
    first = InstanceLock(path)
    first.acquire()

    second = InstanceLock(path)
    with pytest.raises(AlreadyRunningError, match=str(os.getpid())):
        second.acquire()

    first.release()


def test_stale_lock_from_dead_process_is_taken_over(tmp_path):
    # クラッシュ後に二度と起動できなくなるのを避ける
    path = tmp_path / "bridge.pid"
    path.write_text("999999", encoding="utf-8")  # 存在しない PID

    lock = InstanceLock(path)
    lock.acquire()
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    lock.release()


def test_garbage_lock_file_is_taken_over(tmp_path):
    path = tmp_path / "bridge.pid"
    path.write_text("これは PID ではない", encoding="utf-8")
    lock = InstanceLock(path)
    lock.acquire()
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    lock.release()


def test_release_does_not_remove_another_instances_lock(tmp_path):
    path = tmp_path / "bridge.pid"
    lock = InstanceLock(path)
    lock.acquire()
    # 別インスタンスが奪った状況を模す
    path.write_text("123456", encoding="utf-8")
    lock.release()
    assert path.exists()


def test_context_manager_releases(tmp_path):
    path = tmp_path / "bridge.pid"
    with InstanceLock(path):
        assert path.exists()
    assert not path.exists()


# --------------------------------------------------------------------------
# セッション記録の再読み込み（多重起動時の保険）
# --------------------------------------------------------------------------

def test_store_sees_records_written_by_another_process(tmp_path):
    """別プロセスが書いた記録を、既存インスタンスが読み直せること。

    これが効かないと continued=False のまま --session-id で起動し、
    "Session ID ... is already in use" になる（実際に起きた不具合）。
    """
    path = tmp_path / "sessions.json"
    a = SessionStore(path)
    b = SessionStore(path)

    assert a.is_started("C1", "1.0") is False
    b.record("C1", "1.0", "sid", "proj")          # 別プロセス相当
    assert a.is_started("C1", "1.0") is True      # 読み直して見えること


def test_store_reload_keeps_own_records(tmp_path):
    path = tmp_path / "sessions.json"
    a = SessionStore(path)
    b = SessionStore(path)
    a.record("C1", "1.0", "sid-a", "proj")
    b.record("C1", "2.0", "sid-b", "proj")
    # 双方の記録が残っている（後勝ちで消えない）
    assert a.is_started("C1", "1.0") is True
    assert a.is_started("C1", "2.0") is True


# --------------------------------------------------------------------------
# フォールバックの目印
# --------------------------------------------------------------------------

def test_error_markers_match_actual_cli_messages():
    # 実際に CLI が返した文言（退行検知用に固定しておく）
    missing = "No conversation found with session ID: 078f3e48-f26b-444a-82b9-526dbeaa441a"
    exists = "Error: Session ID f7d4e7bb-bf19-52eb-8648-2e5e5cfae4a9 is already in use."
    assert SESSION_MISSING_MARKER in missing
    assert SESSION_EXISTS_MARKER in exists
