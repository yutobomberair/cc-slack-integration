"""設定の読み込みと検証。

環境変数（.env）と config/projects.yaml を読み、起動時に妥当性を検証する。
不正な設定は起動時に例外で落とす方針にしている（実行時に黙って誤ったプロジェクトを
操作するより、起動時に落ちたほうが安全なため）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "projects.yaml"

PLACEHOLDER_PREFIX = "C_REPLACE_ME"


class ConfigError(Exception):
    """設定ファイル・環境変数の不備。"""


@dataclass(frozen=True)
class PermissionProfile:
    """Claude Code CLI に渡す権限設定（CLAUDE.md §20）。"""

    key: str
    description: str = ""
    tools: list[str] | None = None
    permission_mode: str | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    channel_id: str
    working_directory: Path
    profile: str
    # 依頼文で implement へ昇格することを許すか（CLAUDE.md §20「明示的に許可」）。
    # プロジェクト側の許可と依頼文のキーワードの両方が揃って初めて書き込みが有効になる。
    allow_implement: bool = False
    # コミット後に claude/<スレッド> ブランチへ push するか（CLAUDE.md §21）。
    # 既定ブランチへは絶対に push しない。merge も行わない。
    allow_push: bool = False


@dataclass(frozen=True)
class Runtime:
    timeout_seconds: int = 1800
    max_workers: int = 1
    model: str | None = None
    # 依頼文の先頭で指定できるモデル。未知の名前は実行前に弾く。
    allowed_models: tuple[str, ...] = ("opus", "sonnet", "haiku", "fable")
    # Slack スレッドと Claude Code セッションを紐付けるか（CLAUDE.md §11, §23.2）
    session_continuation: bool = True
    session_store_path: Path = BASE_DIR / "state" / "sessions.json"
    # _share/sent/ に退避した送信済みファイルを保持する日数。0 で送信後すぐ削除。
    share_retention_days: int = 7
    # スレッドごとの作業階層（cd:）。プロジェクト配下の相対表記を持つ。
    workdir_store_path: Path = BASE_DIR / "state" / "workdirs.json"
    # 出力ファイルの一覧（スレッド -> パス）。共有の番号を解決するために使う。
    artifact_store_path: Path = BASE_DIR / "state" / "artifacts.json"
    # 実行中の経過表示を chat.update で更新する間隔（秒）。0 で無効。
    progress_interval_seconds: float = 30.0
    # MCP サーバを読み込まない（--strict-mcp-config）。
    # 無人実行では使えないうえ、呼びに行って拒否されるだけ無駄なので既定で有効。
    strict_mcp_config: bool = True
    # push 後に GitHub Actions の完了を待つ上限（秒）。0 で待たない。
    ci_wait_seconds: int = 900
    ci_poll_interval_seconds: int = 15


@dataclass(frozen=True)
class Settings:
    bot_token: str
    app_token: str
    projects: dict[str, Project]          # key -> Project
    profiles: dict[str, PermissionProfile]
    runtime: Runtime
    # GitHub Actions の結果取得に使う PAT（CLAUDE.md §22）。未設定なら CI 通知を行わない。
    github_token: str = ""

    def project_for_channel(self, channel_id: str) -> Project | None:
        for project in self.projects.values():
            if project.channel_id == channel_id:
                return project
        return None

    def profile_for(self, project: Project) -> PermissionProfile:
        try:
            return self.profiles[project.profile]
        except KeyError as exc:  # 起動時検証を通っていれば到達しない
            raise ConfigError(
                f"project '{project.key}' が未定義の profile '{project.profile}' を参照しています"
            ) from exc


def load_settings(config_path: Path | None = None) -> Settings:
    load_dotenv(BASE_DIR / ".env")

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not bot_token:
        raise ConfigError("環境変数 SLACK_BOT_TOKEN が未設定です（.env を確認してください）")
    if not app_token:
        raise ConfigError(
            "環境変数 SLACK_APP_TOKEN が未設定です。Socket Mode には xapp- で始まる "
            "App-Level Token（scope: connections:write）が必要です"
        )
    if not app_token.startswith("xapp-"):
        raise ConfigError("SLACK_APP_TOKEN は xapp- で始まる App-Level Token である必要があります")

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    profiles = _parse_profiles(raw.get("permission_profiles") or {})
    projects = _parse_projects(raw.get("projects") or {}, profiles)
    runtime = _parse_runtime(raw.get("runtime") or {})

    return Settings(
        bot_token=bot_token,
        app_token=app_token,
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        projects=projects,
        profiles=profiles,
        runtime=runtime,
    )


def _parse_profiles(raw: dict) -> dict[str, PermissionProfile]:
    if not raw:
        raise ConfigError("permission_profiles が空です")
    profiles: dict[str, PermissionProfile] = {}
    for key, body in raw.items():
        body = body or {}
        profiles[key] = PermissionProfile(
            key=key,
            description=body.get("description", ""),
            tools=body.get("tools"),
            permission_mode=body.get("permission_mode"),
            disallowed_tools=list(body.get("disallowed_tools") or []),
            max_budget_usd=body.get("max_budget_usd"),
        )
    return profiles


def _parse_projects(raw: dict, profiles: dict[str, PermissionProfile]) -> dict[str, Project]:
    if not raw:
        raise ConfigError("projects が空です")

    projects: dict[str, Project] = {}
    seen_channels: dict[str, str] = {}

    for key, body in raw.items():
        body = body or {}
        channel_id = str(body.get("channel_id", "")).strip()
        working_directory = str(body.get("working_directory", "")).strip()

        if not channel_id:
            raise ConfigError(f"project '{key}': channel_id が未設定です")
        if channel_id.startswith(PLACEHOLDER_PREFIX):
            raise ConfigError(
                f"project '{key}': channel_id がプレースホルダのままです。"
                " `python scripts/list_channels.py` で実際の channel_id を調べて "
                "config/projects.yaml を更新してください"
            )
        if channel_id in seen_channels:
            raise ConfigError(
                f"channel_id '{channel_id}' が project '{seen_channels[channel_id]}' と "
                f"'{key}' で重複しています。1チャンネル1プロジェクトにしてください"
            )
        seen_channels[channel_id] = key

        if not working_directory:
            raise ConfigError(f"project '{key}': working_directory が未設定です")
        path = Path(working_directory)
        if not path.is_dir():
            raise ConfigError(
                f"project '{key}': working_directory が存在しません: {path}"
            )

        profile = body.get("profile") or "investigate"
        if profile not in profiles:
            raise ConfigError(
                f"project '{key}': 未定義の profile '{profile}' を参照しています。"
                f" 定義済み: {sorted(profiles)}"
            )

        allow_implement = bool(body.get("allow_implement", False))
        if allow_implement and "implement" not in profiles:
            raise ConfigError(
                f"project '{key}': allow_implement が true ですが implement プロファイルが"
                " permission_profiles に定義されていません"
            )

        allow_push = bool(body.get("allow_push", False))
        if allow_push and not allow_implement:
            raise ConfigError(
                f"project '{key}': allow_push は allow_implement が true のときだけ"
                " 意味を持ちます（コード変更しなければ push するものが無いため）"
            )

        projects[key] = Project(
            key=key,
            name=body.get("name") or key,
            channel_id=channel_id,
            working_directory=path.resolve(),
            profile=profile,
            allow_implement=allow_implement,
            allow_push=allow_push,
        )

    return projects


def _parse_runtime(raw: dict) -> Runtime:
    store = raw.get("session_store_path")
    return Runtime(
        timeout_seconds=int(raw.get("timeout_seconds") or 1800),
        max_workers=max(1, int(raw.get("max_workers") or 1)),
        model=raw.get("model"),
        allowed_models=tuple(
            raw.get("allowed_models") or ("opus", "sonnet", "haiku", "fable")
        ),
        session_continuation=bool(raw.get("session_continuation", True)),
        session_store_path=(Path(store) if store else BASE_DIR / "state" / "sessions.json"),
        share_retention_days=int(raw.get("share_retention_days", 7)),
        workdir_store_path=BASE_DIR / "state" / "workdirs.json",
        artifact_store_path=BASE_DIR / "state" / "artifacts.json",
        progress_interval_seconds=float(raw.get("progress_interval_seconds") or 30.0),
        strict_mcp_config=bool(raw.get("strict_mcp_config", True)),
        ci_wait_seconds=int(raw.get("ci_wait_seconds", 900)),
        ci_poll_interval_seconds=max(5, int(raw.get("ci_poll_interval_seconds", 15))),
    )
