"""スレッド継続（Phase 2）を Slack 抜きで検証する。

同じ (channel_id, thread_ts) から導出した session_id で2回実行し、2回目が1回目の
文脈を引き継げているかを確認する。

    python scripts/smoke_session.py propose
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app import claude_runner  # noqa: E402
from app.config import DEFAULT_CONFIG_PATH, _parse_profiles, _parse_runtime  # noqa: E402
from app.console import force_utf8_stdio  # noqa: E402
from app.session_store import SessionStore, derive_session_id  # noqa: E402

CHANNEL_ID = "C_SMOKE_TEST"
THREAD_TS = "1726790400.000001"
SECRET = "7391"


def main() -> int:
    force_utf8_stdio()
    project_key = sys.argv[1] if len(sys.argv) > 1 else "propose"

    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    body = (raw.get("projects") or {}).get(project_key)
    if body is None:
        print(f"未知のプロジェクト: {project_key}", file=sys.stderr)
        return 1

    profiles = _parse_profiles(raw.get("permission_profiles") or {})
    runtime = _parse_runtime(raw.get("runtime") or {})
    profile = profiles[body.get("profile") or "investigate"]
    cwd = Path(body["working_directory"])

    store = SessionStore(Path(".") / "state" / "smoke_sessions.json")
    store.forget(CHANNEL_ID, THREAD_TS)  # 毎回まっさらから始める

    session_id = derive_session_id(CHANNEL_ID, THREAD_TS)
    print(f"project    : {body.get('name', project_key)}")
    print(f"cwd        : {cwd}")
    print(f"session_id : {session_id}  (thread_ts={THREAD_TS} から導出)")
    print("=" * 72)

    # --- 1回目: 新規セッション -------------------------------------------
    started = store.is_started(CHANNEL_ID, THREAD_TS)
    print(f"[1回目] is_started={started} → {'--resume' if started else '--session-id'}")
    first = claude_runner.run(
        prompt=f"この会話の合言葉は {SECRET} です。覚えておいてください。「了解」とだけ短く返答して。",
        working_directory=cwd,
        profile=profile,
        runtime=runtime,
        session_id=session_id,
        resume=started,
    )
    if not first.ok:
        print("FAILED:", first.error)
        return 1
    print(f"  応答: {first.text.strip()[:100]}")
    store.record(CHANNEL_ID, THREAD_TS, session_id, project_key)

    # --- 2回目: 同じスレッド → 継続 --------------------------------------
    started = store.is_started(CHANNEL_ID, THREAD_TS)
    print(f"\n[2回目] is_started={started} → {'--resume' if started else '--session-id'}")
    second = claude_runner.run(
        prompt="この会話の合言葉は何でしたか？ 数字だけを答えてください。",
        working_directory=cwd,
        profile=profile,
        runtime=runtime,
        session_id=session_id,
        resume=started,
    )
    if not second.ok:
        print("FAILED:", second.error)
        return 1
    answer = second.text.strip()
    print(f"  応答: {answer[:100]}")
    print(f"  session_restarted: {second.session_restarted}")

    print("=" * 72)
    if SECRET in answer and not second.session_restarted:
        print(f"PASS: 2回目が1回目の文脈を引き継いでいます（合言葉 {SECRET} を想起）")
        return 0
    print(f"FAIL: 文脈が引き継がれていません（期待 {SECRET} / 実際 {answer[:60]}）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
