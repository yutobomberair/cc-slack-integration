"""Claude Code の出力を Slack 向けに整形する（CLAUDE.md §13）。

Slack は Markdown ではなく mrkdwn を解釈する。素の Markdown をそのまま投げると
見出しが `##` のまま残り、`**強調**` はアスタリスクが見えたままになるため変換する。

さらに Slack の1メッセージは実用上 3000 文字前後で頭打ちになるので、コードブロックを
壊さないように分割する。
"""

from __future__ import annotations

import re

MAX_CHUNK = 2900

_FENCE_RE = re.compile(r"```")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def to_mrkdwn(text: str) -> str:
    """Markdown を Slack の mrkdwn へ変換する。コードブロック内は一切変換しない。"""
    out: list[str] = []
    in_code = False

    for line in (text or "").splitlines():
        if _FENCE_RE.match(line.strip()):
            in_code = not in_code
            out.append(line)
            continue
        out.append(line if in_code else _convert_line(line))

    if in_code:  # 出力が途中で切れてフェンスが閉じていない場合に備える
        out.append("```")
    return "\n".join(out)


def _convert_line(line: str) -> str:
    heading = _HEADING_RE.match(line)
    if heading:
        return f"*{_inline(heading.group(1))}*"

    bullet = _BULLET_RE.match(line)
    if bullet:
        indent = bullet.group(1)
        return f"{indent}• {_inline(line[bullet.end():])}"

    return _inline(line)


def _inline(text: str) -> str:
    text = _LINK_RE.sub(r"<\2|\1>", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _BOLD_UNDERSCORE_RE.sub(r"*\1*", text)
    return text


def split_for_slack(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Slack の文字数上限に収まるよう分割する。

    行単位で詰め、コードブロックの途中で切れる場合は閉じフェンスを補い、次のチャンクの
    先頭でフェンスを開き直す。分割しても貼り付けたコードが壊れない。
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text.strip() else []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    in_code = False        # 現在の行位置がコードブロック内か
    chunk_opened_in_code = False  # このチャンクを開いた時点でコード内だったか

    def flush(still_in_code: bool) -> None:
        nonlocal current, current_len, chunk_opened_in_code
        if not current:
            return
        body = "\n".join(current)
        if still_in_code:
            body += "\n```"
        chunks.append(body)
        current = []
        current_len = 0
        chunk_opened_in_code = still_in_code

    for raw_line in text.splitlines():
        # 1行が単体で上限を超える場合は強制的に切る
        pieces = _hard_wrap(raw_line, limit - 8)
        for line in pieces:
            if current_len + len(line) + 1 > limit and current:
                flush(in_code)
                if chunk_opened_in_code:
                    current.append("```")
                    current_len += 4
            current.append(line)
            current_len += len(line) + 1
            if _FENCE_RE.match(line.strip()):
                in_code = not in_code

    flush(False)
    return [c for c in chunks if c.strip()]


def _hard_wrap(line: str, width: int) -> list[str]:
    if len(line) <= width:
        return [line]
    return [line[i : i + width] for i in range(0, len(line), width)]


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{rest:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分"


def _mode_label(implement: bool) -> str:
    """実行モードの明示。書き込みが走っていることを一目で分かるようにする。"""
    return ":pencil2: 実装モード" if implement else ":mag: 調査モード"


def start_message(
    project_name: str, continued: bool = False, implement: bool = False
) -> str:
    """実行開始時の1行目。以降 chat.update で書き換えていく（CLAUDE.md §12）。"""
    mode = "スレッドの会話を継続します" if continued else "処理を開始しました"
    return (
        f":hourglass_flowing_sand: {mode}。\n"
        f"Project: *{project_name}* / {_mode_label(implement)}"
    )


def progress_message(
    project_name: str, elapsed: float, continued: bool = False, implement: bool = False
) -> str:
    """実行中の経過表示。start_message を chat.update で置き換える形で使う。"""
    mode = "継続実行中" if continued else "実行中"
    return (
        f":hourglass_flowing_sand: {mode}… （{format_duration(elapsed)} 経過）\n"
        f"Project: *{project_name}* / {_mode_label(implement)}"
    )


def completed_header(
    project_name: str,
    elapsed: float,
    cost_usd: float | None = None,
    turns: int | None = None,
) -> str:
    """完了時に開始メッセージを置き換えるヘッダ。"""
    stats = [format_duration(elapsed)]
    if turns is not None:
        stats.append(f"{turns}ターン")
    if cost_usd is not None:
        stats.append(f"${cost_usd:.2f}")
    return (
        f":white_check_mark: 完了しました。（{' / '.join(stats)}）\n"
        f"Project: *{project_name}*"
    )


def failed_header(project_name: str, elapsed: float) -> str:
    return (
        f":x: 実行に失敗しました。（{format_duration(elapsed)}）\n"
        f"Project: *{project_name}*"
    )


def commit_report(
    branch: str,
    short_sha: str,
    files: list[str],
    stat: str,
    diff: str,
    max_diff_chars: int = 3500,
) -> str:
    """commit 内容の報告（CLAUDE.md §21）。

    push は行わないため（§20）、GitHub のリンクは出さない。未 push のコミットへの
    リンクは 404 になるだけで役に立たない。代わりに差分そのものを貼る。
    """
    listed = "\n".join(f"• `{f}`" for f in files[:20])
    if len(files) > 20:
        listed += f"\n• …ほか {len(files) - 20} ファイル"

    body = (
        f"\n\n:floppy_disk: *変更をコミットしました*\n"
        f"branch: `{branch}` / commit: `{short_sha}`\n"
        f"{listed}\n"
    )
    if stat.strip():
        body += f"```\n{stat.strip()}\n```\n"

    diff = diff.strip()
    if diff:
        if len(diff) > max_diff_chars:
            diff = diff[:max_diff_chars] + "\n…（差分が長いため省略）"
        body += f"```diff\n{diff}\n```"

    body += f"\n:information_source: 取り消す場合は `git revert {short_sha}`。"
    return body


def push_report(
    branch: str,
    commits: list[str],
    compare_url: str | None,
    branch_url: str | None,
    own_commit_count: int = 1,
) -> str:
    """push 結果と PR 作成リンク（CLAUDE.md §21）。

    gh CLI に依存せず compare URL を渡す。スマホから1タップで PR 作成画面が開く。
    merge は行わない（§20）。
    """
    lines = [f"\n\n:rocket: *push しました* → `{branch}`"]

    others = max(0, len(commits) - own_commit_count)
    if others:
        lines.append(
            f"このブランチには今回の変更 {own_commit_count} 件に加え、"
            f"ローカルに残っていた {others} 件のコミットも含まれます。"
        )
        preview = "\n".join(f"  {c}" for c in commits[: own_commit_count + 3])
        if len(commits) > own_commit_count + 3:
            preview += f"\n  …ほか {len(commits) - own_commit_count - 3} 件"
        lines.append(f"```\n{preview}\n```")

    if compare_url:
        lines.append(f":link: <{compare_url}|Pull Request を作成する>")
    if branch_url:
        lines.append(f":mag: <{branch_url}|GitHub でブランチを見る>")
    lines.append(":information_source: merge は行いません。")
    return "\n".join(lines)


def push_failed_note(branch: str, error: str) -> str:
    return (
        f"\n\n:warning: *push に失敗しました* （branch: `{branch}`）\n"
        f"```\n{error.strip()[:800]}\n```\n"
        "コミットはローカルに残っています。手元で `git push` を試してください。"
    )


def push_skipped_note(reason: str) -> str:
    return f"\n\n:information_source: push はしていません（{reason}）。"


def ci_report(runs: list, timed_out: bool = False, error: str = "") -> str:
    """GitHub Actions の結果（CLAUDE.md §22）。

    run ごとに成否とログへのリンクを並べる。失敗したときにログへ1タップで飛べる
    ことが、スマホから使ううえで一番効く。
    """
    if error:
        return f"\n\n:warning: *CI の状態を取得できませんでした*\n```\n{error[:500]}\n```"

    if not runs:
        return "\n\n:information_source: この push に対応する GitHub Actions はありません。"

    lines = []
    if timed_out:
        lines.append("\n\n:hourglass: *CI がまだ終わっていません*（待機時間の上限に達しました）")
    elif all(getattr(r, "succeeded", False) for r in runs):
        lines.append("\n\n:white_check_mark: *CI 成功*")
    else:
        lines.append("\n\n:x: *CI 失敗*")

    for run in runs:
        if not run.is_finished:
            icon, state = ":hourglass_flowing_sand:", run.status
        elif run.succeeded:
            icon, state = ":white_check_mark:", "success"
        else:
            icon, state = ":x:", run.conclusion or "failure"
        entry = f"{icon} `{run.name}` — {state}"
        if run.html_url:
            entry += f" <{run.html_url}|ログを見る>"
        lines.append(entry)

    if any(r.is_finished and not r.succeeded for r in runs):
        lines.append(
            ":information_source: 失敗の原因を調べるなら、このスレッドで "
            "「CI のログを見て原因を調べて」と続けてください。"
        )
    return "\n".join(lines)


def ci_waiting_note() -> str:
    return "\n\n:hourglass_flowing_sand: CI の完了を待っています…"


def no_changes_note() -> str:
    return "\n\n:information_source: ファイルへの変更はありませんでした。"


def skipped_dirty_note(paths: list[str]) -> str:
    """実行前から未コミットだったファイルの注記。"""
    if not paths:
        return ""
    listed = ", ".join(f"`{p}`" for p in paths[:10])
    if len(paths) > 10:
        listed += f" ほか {len(paths) - 10} 件"
    return (
        f"\n\n:warning: 実行前から未コミットだった {listed} は、"
        "作業中のものとみなしてコミットしていません。"
    )


def commit_failed_note(error: str) -> str:
    return (
        "\n\n:warning: *ファイルは変更されましたが、コミットに失敗しました。*\n"
        f"```\n{error.strip()[:800]}\n```\n"
        "変更は作業ツリーに残っています。手元で `git status` を確認してください。"
    )


def session_restarted_note() -> str:
    return (
        "\n\n:arrows_counterclockwise: 以前の会話履歴が見つからなかったため、"
        "新しいセッションとして実行しました。スレッド内の文脈は引き継がれていません。"
    )


def success_message(project_name: str, body: str) -> str:
    return f":white_check_mark: 完了しました。\nProject: *{project_name}*\n\n{body}"


def failure_message(project_name: str, error: str) -> str:
    return (
        ":x: Claude Code の実行に失敗しました。\n"
        f"Project: *{project_name}*\n\n"
        f"Error:\n```\n{error}\n```"
    )


def denials_note(denials: list) -> str:
    """権限拒否があった場合の注記（CLAUDE.md §20 の可視化）。"""
    if not denials:
        return ""
    names = []
    for d in denials:
        if isinstance(d, dict):
            names.append(str(d.get("tool_name") or d.get("tool") or d))
        else:
            names.append(str(d))
    unique = sorted(set(names))
    return (
        "\n\n:lock: 権限設定により次の操作は実行されませんでした: "
        + ", ".join(f"`{n}`" for n in unique)
    )
