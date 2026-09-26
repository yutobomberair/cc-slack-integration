import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.claude_runner import build_command  # noqa: E402
from app.config import PermissionProfile, Runtime  # noqa: E402
from app.session_store import SessionStore, derive_session_id  # noqa: E402
from app.concurrency import ThreadLocks  # noqa: E402


def test_session_id_is_deterministic_per_thread():
    a = derive_session_id("C111", "1726790400.123456")
    b = derive_session_id("C111", "1726790400.123456")
    assert a == b


def test_different_threads_get_different_sessions():
    a = derive_session_id("C111", "1726790400.123456")
    b = derive_session_id("C111", "1726790401.000000")
    assert a != b


def test_same_thread_ts_in_different_channels_differs():
    a = derive_session_id("C111", "1726790400.123456")
    b = derive_session_id("C222", "1726790400.123456")
    assert a != b


def test_session_id_is_a_valid_uuid():
    import uuid

    uuid.UUID(derive_session_id("C111", "1726790400.123456"))


def test_store_starts_empty_and_records(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    assert store.is_started("C111", "1.0") is False
    store.record("C111", "1.0", derive_session_id("C111", "1.0"), "propose")
    assert store.is_started("C111", "1.0") is True


def test_store_survives_restart(tmp_path):
    path = tmp_path / "sessions.json"
    SessionStore(path).record("C111", "1.0", "sid", "propose")
    assert SessionStore(path).is_started("C111", "1.0") is True


def test_store_forget_removes_entry(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.record("C111", "1.0", "sid", "propose")
    store.forget("C111", "1.0")
    assert store.is_started("C111", "1.0") is False


def test_store_evicts_beyond_capacity(tmp_path):
    store = SessionStore(tmp_path / "sessions.json", max_entries=3)
    for i in range(5):
        store.record("C111", f"{i}.0", f"sid{i}", "propose")
    started = [store.is_started("C111", f"{i}.0") for i in range(5)]
    assert sum(started) == 3
    assert started[-1] is True  # 最新は必ず残る


def test_corrupt_store_file_does_not_crash(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = SessionStore(path)
    assert store.is_started("C111", "1.0") is False
    store.record("C111", "1.0", "sid", "propose")
    assert store.is_started("C111", "1.0") is True


def test_new_session_uses_session_id_flag():
    cmd = build_command(
        "claude.exe", PermissionProfile(key="investigate"), Runtime(),
        session_id="abc-123", resume=False,
    )
    assert cmd[cmd.index("--session-id") + 1] == "abc-123"
    assert "--resume" not in cmd


def test_continued_session_uses_resume_flag():
    cmd = build_command(
        "claude.exe", PermissionProfile(key="investigate"), Runtime(),
        session_id="abc-123", resume=True,
    )
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in cmd


def test_thread_locks_are_per_thread():
    locks = ThreadLocks()
    a = locks.for_thread("C111", "1.0")
    assert locks.for_thread("C111", "1.0") is a          # 同じスレッド → 同じロック
    assert locks.for_thread("C111", "2.0") is not a      # 別スレッド → 別ロック
    assert locks.for_thread("C222", "1.0") is not a      # 別チャンネル → 別ロック


def test_thread_lock_serializes_same_thread():
    locks = ThreadLocks()
    order: list[str] = []
    first_held = threading.Event()
    release = threading.Event()

    def hold():
        with locks.for_thread("C111", "1.0"):
            order.append("first-in")
            first_held.set()
            release.wait(timeout=2)
            order.append("first-out")

    def follow():
        first_held.wait(timeout=2)
        with locks.for_thread("C111", "1.0"):
            order.append("second-in")

    t1 = threading.Thread(target=hold)
    t2 = threading.Thread(target=follow)
    t1.start()
    t2.start()
    first_held.wait(timeout=2)
    release.set()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert order == ["first-in", "first-out", "second-in"]
