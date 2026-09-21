"""Slack を経由せずに「プロジェクト選択 → Claude Code 実行 → Slack整形」を検証する。

channel_id がまだ埋まっていない段階でも、ブリッジの Claude 側half を確認できる。

使い方:
    python scripts/smoke_test.py navigation-core "主要なディレクトリと役割をまとめて。"
    python scripts/smoke_test.py --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app import claude_runner, formatting  # noqa: E402
from app.console import force_utf8_stdio  # noqa: E402
from app.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    PermissionProfile,
    _parse_profiles,
    _parse_runtime,
)


def main() -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", nargs="?", help="projects.yaml のプロジェクトキー")
    parser.add_argument("prompt", nargs="?", help="Claude Code へ渡す依頼")
    parser.add_argument("--list", action="store_true", help="登録プロジェクトを一覧表示")
    args = parser.parse_args()

    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    projects = raw.get("projects") or {}
    profiles = _parse_profiles(raw.get("permission_profiles") or {})
    runtime = _parse_runtime(raw.get("runtime") or {})

    if args.list or not args.project:
        print(f"{'key':<18}{'profile':<13}working_directory")
        print("-" * 72)
        for key, body in projects.items():
            print(f"{key:<18}{(body.get('profile') or 'investigate'):<13}{body['working_directory']}")
        return 0 if args.list else 1

    if args.project not in projects:
        print(f"未知のプロジェクト: {args.project}（--list で一覧）", file=sys.stderr)
        return 1
    if not args.prompt:
        print("prompt を指定してください", file=sys.stderr)
        return 1

    body = projects[args.project]
    cwd = Path(body["working_directory"])
    if not cwd.is_dir():
        print(f"working_directory が存在しません: {cwd}", file=sys.stderr)
        return 1

    profile: PermissionProfile = profiles[body.get("profile") or "investigate"]

    print(f"project : {body.get('name', args.project)}")
    print(f"cwd     : {cwd}")
    print(f"profile : {profile.key} ({profile.description})")
    print(f"tools   : {profile.tools if profile.tools is not None else '(default)'}")
    print("-" * 72)

    result = claude_runner.run(
        prompt=args.prompt, working_directory=cwd, profile=profile, runtime=runtime
    )

    if not result.ok:
        print("FAILED:", result.error)
        return 1

    print(f"session_id : {result.session_id}")
    print(f"turns      : {result.num_turns}   cost: ${result.cost_usd:.4f}   "
          f"duration: {result.duration_ms}ms")
    print(f"denials    : {result.permission_denials}")
    print("-" * 72)
    print("[Slack へ投稿される内容]")
    text = formatting.success_message(
        body.get("name", args.project),
        formatting.to_mrkdwn(result.text) + formatting.denials_note(result.permission_denials),
    )
    for i, chunk in enumerate(formatting.split_for_slack(text), 1):
        print(f"--- message {i} ({len(chunk)} chars) ---")
        print(chunk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
