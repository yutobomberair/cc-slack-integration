"""Claude Code CLI をヘッドレス実行する（CLAUDE.md §8, §9, §20）。

設計上の要点:

* 出力は必ず ``--output-format json`` で受ける。result / session_id / is_error /
  permission_denials が1回の実行でまとめて取れる。
* ``--permission-prompts none`` を必ず付ける。これが無いと「承認が必要な操作」に
  遭遇した際の挙動が無人実行では不定になる。付けておけば自動的に拒否され、
  拒否内容は permission_denials に載る。
* ``--dangerously-skip-permissions`` は使わない（CLAUDE.md §20）。
* Windows では既定の標準出力エンコーディングが cp932 のため、必ず utf-8 を明示する。
  指定しないと日本語を含む調査結果が化けるか UnicodeDecodeError になる。
* プロンプトは argv ではなく stdin から渡す。Windows のコマンドライン長制限
  (32767 文字) とクォート処理の両方を回避できる。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.config import PermissionProfile, Runtime

logger = logging.getLogger(__name__)


class ClaudeNotFoundError(RuntimeError):
    """claude CLI が PATH 上に見つからない。"""


@dataclass
class ClaudeResult:
    ok: bool
    text: str = ""
    error: str = ""
    session_id: str | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    permission_denials: list = field(default_factory=list)
    # 継続に失敗し、新規セッションとしてやり直した場合に True
    session_restarted: bool = False


def resolve_cli() -> str:
    path = shutil.which("claude")
    if not path:
        raise ClaudeNotFoundError(
            "claude CLI が PATH 上に見つかりません。Claude Code をインストールし、"
            "実行ユーザーの PATH に含まれていることを確認してください。"
        )
    return path


def build_command(
    cli: str,
    profile: PermissionProfile,
    runtime: Runtime,
    session_id: str | None = None,
    resume: bool = False,
    model: str | None = None,
) -> list[str]:
    """CLI 引数を組み立てる（プロンプトは stdin で渡すので argv には含めない）。

    ``resume=False`` なら ``--session-id`` で新しいセッションを作り、``True`` なら
    ``--resume`` で既存セッションを継続する。同じ UUID を両者で使い分ける。
    """
    cmd = [cli, "-p", "--output-format", "json", "--permission-prompts", "none"]

    if runtime.strict_mcp_config:
        # MCP サーバを一切読み込まない。
        #
        # アカウント単位で有効な MCP サーバ（claude.ai Claude Docs など）は、
        # --tools で built-in ツールを絞っても読み込まれる。無人実行では
        # --permission-prompts none により自動拒否されるため実害は無いが、
        # Claude が毎回それらを呼びに行って失敗し、ターンと課金を無駄にしたうえ
        # Slack の返信に拒否の注記が並ぶ。
        #
        # --mcp-config を渡していないので、このフラグは「MCP サーバ無し」を意味する。
        # 特定プロジェクトで MCP が必要になったら --mcp-config を併用する。
        cmd += ["--strict-mcp-config"]

    if profile.tools is not None:
        # 読み取り専用プロファイルの本体。Bash / Edit / Write を含めなければ
        # コード変更もシェル実行も構造的に起こりえない。
        cmd += ["--tools", ",".join(profile.tools)]
    if profile.permission_mode:
        cmd += ["--permission-mode", profile.permission_mode]
    for pattern in profile.disallowed_tools:
        cmd += ["--disallowedTools", pattern]
    if profile.max_budget_usd:
        cmd += ["--max-budget-usd", str(profile.max_budget_usd)]
    # 依頼文での指定があればそれを優先し、無ければ設定の既定モデル。
    chosen_model = model or runtime.model
    if chosen_model:
        cmd += ["--model", chosen_model]
    if session_id:
        # スレッド単位のセッション継続（CLAUDE.md §11, §23.2）。
        cmd += (["--resume", session_id] if resume else ["--session-id", session_id])

    return cmd


SESSION_MISSING_MARKER = "No conversation found with session ID"
SESSION_EXISTS_MARKER = "is already in use"


def run(
    prompt: str,
    working_directory: Path,
    profile: PermissionProfile,
    runtime: Runtime,
    session_id: str | None = None,
    resume: bool = False,
    model: str | None = None,
) -> ClaudeResult:
    """Claude Code を1回実行する。

    継続に失敗した場合（セッションファイルが消えている等）は、黙って落とさずに
    新規セッションとして1度だけやり直す。スレッドでの会話が復旧不能になるより、
    文脈を失ってでも応答を返すほうが実用的なため。
    """
    result = _run_once(prompt, working_directory, profile, runtime, session_id, resume, model)

    if resume and not result.ok and SESSION_MISSING_MARKER in result.error:
        logger.info("継続対象のセッションが見つかりません。新規セッションで再実行します")
        result = _run_once(prompt, working_directory, profile, runtime, session_id, False, model)
        result.session_restarted = True

    elif not resume and not result.ok and SESSION_EXISTS_MARKER in result.error:
        # 記録側は「未作成」と思っているが、実際には既に存在するケース。
        # 多重起動やブリッジの異常終了で起こりうる。継続として実行し直す。
        logger.info("セッションが既に存在します。継続として再実行します")
        result = _run_once(prompt, working_directory, profile, runtime, session_id, True, model)

    return result


def _run_once(
    prompt: str,
    working_directory: Path,
    profile: PermissionProfile,
    runtime: Runtime,
    session_id: str | None,
    resume: bool,
    model: str | None = None,
) -> ClaudeResult:
    cli = resolve_cli()
    cmd = build_command(cli, profile, runtime, session_id=session_id, resume=resume, model=model)

    logger.info(
        "claude 実行開始 cwd=%s profile=%s timeout=%ss session=%s",
        working_directory,
        profile.key,
        runtime.timeout_seconds,
        "resume" if resume else "new",
    )

    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(working_directory),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=runtime.timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("claude 実行がタイムアウトしました (%ss)", runtime.timeout_seconds)
        return ClaudeResult(
            ok=False,
            error=f"実行が制限時間 {runtime.timeout_seconds} 秒を超えたため中断しました。",
        )
    except OSError as exc:
        logger.exception("claude の起動に失敗しました")
        return ClaudeResult(ok=False, error=f"claude の起動に失敗しました: {exc}")

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    if not stdout:
        return ClaudeResult(
            ok=False,
            error=_tail(stderr) or f"claude が出力を返しませんでした (exit={completed.returncode})",
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ClaudeResult(
            ok=False,
            error=(
                f"claude の出力を JSON として解釈できませんでした (exit={completed.returncode})\n"
                + _tail(stdout)
            ),
        )

    result = ClaudeResult(
        ok=not payload.get("is_error", False) and completed.returncode == 0,
        text=payload.get("result") or "",
        session_id=payload.get("session_id"),
        duration_ms=payload.get("duration_ms"),
        cost_usd=payload.get("total_cost_usd"),
        num_turns=payload.get("num_turns"),
        permission_denials=payload.get("permission_denials") or [],
    )
    if not result.ok and not result.error:
        result.error = result.text or _tail(stderr) or "Claude Code がエラーを返しました。"

    logger.info(
        "claude 実行終了 ok=%s turns=%s cost=%s duration_ms=%s denials=%d",
        result.ok,
        result.num_turns,
        result.cost_usd,
        result.duration_ms,
        len(result.permission_denials),
    )
    return result


def _tail(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…\n" + text[-limit:]
