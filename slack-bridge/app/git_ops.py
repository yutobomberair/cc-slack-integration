"""Bridge 側から git を操作する（CLAUDE.md §20, §21）。

Claude Code にも Bash を与えているので、Claude 自身が git を叩くこともできる。
それでも Bridge 側で commit するのは、commit を Claude の判断に任せると
「編集したが commit し忘れた」が起きるため。Claude が自分で commit まで済ませた
場合は Bridge から見た差分が無いので、二重コミットにはならない。

実行前後で ``git status --porcelain`` を比較し、**この実行で変化したファイルだけ**を
commit する。実行前から dirty だったファイルは利用者の作業中のものなので触らない。

push は ``allow_push: true`` のプロジェクトでのみ、``claude/<スレッド>`` ブランチへ
行う（``push_to_branch``）。既定ブランチへは push せず、merge もしない。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    """git コマンドが失敗した。"""


@dataclass
class Snapshot:
    """実行前の作業ツリーの状態。"""

    head: str | None
    entries: dict[str, str] = field(default_factory=dict)  # path -> porcelain status


@dataclass
class CommitResult:
    committed: bool
    sha: str | None = None
    short_sha: str | None = None
    files: list[str] = field(default_factory=list)
    stat: str = ""
    diff: str = ""
    skipped_dirty: list[str] = field(default_factory=list)
    message: str = ""


def _run(cwd: Path, *args: str, check: bool = True, strip: bool = True) -> str:
    """git を実行して標準出力を返す。

    ``strip=False`` は ``status --porcelain`` 用。porcelain の各行は先頭2文字が
    状態コードで、未ステージの変更は先頭が空白になる。出力全体を strip すると
    1行目の先頭空白が落ちてパスが1文字欠けるため、status では strip しない。
    """
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if check and completed.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} が失敗しました (exit={completed.returncode})\n"
            + (completed.stderr or completed.stdout or "").strip()
        )
    out = completed.stdout or ""
    return out.strip() if strip else out


def is_repo(cwd: Path) -> bool:
    try:
        return _run(cwd, "rev-parse", "--is-inside-work-tree") == "true"
    except (GitError, OSError):
        return False


def current_branch(cwd: Path) -> str:
    try:
        return _run(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    except GitError:
        return "(不明)"


def _porcelain(cwd: Path) -> dict[str, str]:
    """パス -> 状態コード。リネームは変更後のパスで記録する。"""
    entries: dict[str, str] = {}
    for line in _run(cwd, "status", "--porcelain", "-uall", strip=False).splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:]
        if " -> " in path:  # "R  old -> new"
            path = path.split(" -> ", 1)[1]
        entries[path.strip('"')] = status
    return entries


def snapshot(cwd: Path) -> Snapshot:
    head = None
    try:
        head = _run(cwd, "rev-parse", "HEAD")
    except GitError:
        pass  # コミットが1つも無いリポジトリ
    return Snapshot(head=head, entries=_porcelain(cwd))


def changed_since(cwd: Path, before: Snapshot) -> tuple[list[str], list[str]]:
    """(この実行で変化したパス, 実行前から dirty だったパス) を返す。"""
    after = _porcelain(cwd)
    changed = [p for p, st in after.items() if before.entries.get(p) != st]
    preexisting = [p for p in after if p in before.entries and p not in changed]
    return sorted(changed), sorted(preexisting)


def commit_changes(cwd: Path, paths: list[str], message: str) -> CommitResult:
    """指定パスだけを commit する。"""
    if not paths:
        return CommitResult(committed=False)

    _run(cwd, "add", "--", *paths)
    # ステージ後に差分が無い場合（内容が元に戻された等）は commit しない
    if not _run(cwd, "diff", "--cached", "--name-only", "--", *paths):
        logger.info("ステージした変更が空のため commit しません")
        return CommitResult(committed=False)

    _run(cwd, "commit", "-m", message, "--", *paths)
    sha = _run(cwd, "rev-parse", "HEAD")

    return CommitResult(
        committed=True,
        sha=sha,
        short_sha=sha[:8],
        files=paths,
        stat=_run(cwd, "show", "--stat", "--format=", sha, check=False),
        diff=_run(cwd, "show", "--format=", sha, check=False),
        message=message,
    )


@dataclass
class RemoteInfo:
    """リモートの情報。

    ``host``/``owner``/``repo`` は URL を解釈できた場合のみ埋まる。解釈できない
    リモート（ローカルパス、独自ホスト等）でも push 自体は可能なので、
    「リモートが存在するか」と「リンクを組み立てられるか」は別に扱う。
    """

    url: str
    host: str = ""
    owner: str = ""
    repo: str = ""

    @property
    def is_github(self) -> bool:
        return bool(self.host) and self.host.endswith("github.com")

    def compare_url(self, base: str, head: str) -> str | None:
        """PR 作成画面への直リンク。

        gh CLI を前提にしないので、compare URL を組み立てて渡す。スマホから
        1タップで PR 作成画面が開く。
        """
        if not self.is_github:
            return None
        return f"https://github.com/{self.owner}/{self.repo}/compare/{base}...{head}?expand=1"

    def branch_url(self, branch: str) -> str | None:
        if not self.is_github:
            return None
        return f"https://github.com/{self.owner}/{self.repo}/tree/{branch}"


_SSH_RE = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)/(.+?)(?:\.git)?/?$")
_HTTPS_RE = re.compile(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)/(.+?)(?:\.git)?/?$")


def remote_info(cwd: Path, remote: str = "origin") -> RemoteInfo | None:
    """リモートが無いときだけ None を返す。URL を解釈できない場合は url だけ埋める。"""
    try:
        url = _run(cwd, "remote", "get-url", remote)
    except GitError:
        return None
    if not url:
        return None
    for pattern in (_SSH_RE, _HTTPS_RE):
        m = pattern.match(url)
        if m:
            return RemoteInfo(url=url, host=m.group(1), owner=m.group(2), repo=m.group(3))
    return RemoteInfo(url=url)


def default_branch(cwd: Path, remote: str = "origin") -> str:
    """リモートの既定ブランチ名（main / master など）。"""
    try:
        ref = _run(cwd, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
        return ref.split("/", 1)[1] if "/" in ref else ref
    except GitError:
        for candidate in ("main", "master"):
            try:
                _run(cwd, "rev-parse", "--verify", f"refs/remotes/{remote}/{candidate}")
                return candidate
            except GitError:
                continue
        return "main"


def commits_ahead_of(cwd: Path, base_ref: str) -> list[str]:
    """base_ref に含まれていないコミットの一覧（新しい順、"sha 件名"）。"""
    try:
        out = _run(cwd, "log", "--oneline", "--no-decorate", f"{base_ref}..HEAD")
    except GitError:
        return []
    return [line for line in out.splitlines() if line.strip()]


@dataclass
class PushResult:
    pushed: bool
    branch: str = ""
    error: str = ""
    commits: list[str] = field(default_factory=list)
    compare_url: str | None = None
    branch_url: str | None = None


def push_to_branch(
    cwd: Path, branch: str, remote: str = "origin", timeout: int = 180
) -> PushResult:
    """現在の HEAD をリモートの ``branch`` へ push する。

    ローカルのブランチは切り替えない。``HEAD:refs/heads/<branch>`` の形で push する
    ことで、作業ツリーに一切触れずにリモート側へブランチを作れる。

    既定ブランチへは絶対に push しない（CLAUDE.md §20）。merge も行わない。

    非対話であることが重要。認証情報が無い場合に Git Credential Manager の GUI が
    出ると、常駐プロセスがそのまま固まる。環境変数で対話を禁じ、失敗させる。
    """
    info = remote_info(cwd, remote)
    if info is None:
        return PushResult(pushed=False, error=f"リモート '{remote}' が設定されていません")

    base = default_branch(cwd, remote)
    if branch == base:
        # ここへ来る設計ではないが、最後の歯止めとして明示的に拒否する
        return PushResult(
            pushed=False, branch=branch, error=f"既定ブランチ '{base}' への push は行いません"
        )

    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",   # 認証プロンプトで固まらせない
        "GCM_INTERACTIVE": "never",
    }
    completed = subprocess.run(
        ["git", "push", "--force-with-lease", remote, f"HEAD:refs/heads/{branch}"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        shell=False,
    )
    if completed.returncode != 0:
        return PushResult(
            pushed=False,
            branch=branch,
            error=(completed.stderr or completed.stdout or "").strip()[:800],
        )

    return PushResult(
        pushed=True,
        branch=branch,
        commits=commits_ahead_of(cwd, f"{remote}/{base}"),
        compare_url=info.compare_url(base, branch),
        branch_url=info.branch_url(branch),
    )


def thread_branch_name(thread_ts: str, prefix: str = "claude") -> str:
    """Slack スレッドに対応するブランチ名。

    同じスレッドからは常に同じ名前になるので、スレッド内で依頼を重ねると
    1本のブランチにコミットが積まれ、PR も1つで済む。
    """
    seconds = thread_ts.split(".")[0]
    try:
        stamp = datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime("%Y%m%d")
    except (ValueError, OSError, OverflowError):
        stamp = "unknown"
    digest = hashlib.sha1(thread_ts.encode("utf-8")).hexdigest()[:6]
    return f"{prefix}/{stamp}-{digest}"


def build_commit_message(project_name: str, prompt: str) -> str:
    """Slack の依頼文から commit メッセージを作る。

    件名は依頼の1行目を流用し、本文に依頼全文を残す。後から「なぜこの変更が
    入ったのか」を追えるようにするため。
    """
    first_line = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "変更")
    subject = first_line if len(first_line) <= 60 else first_line[:57] + "..."
    return (
        f"{subject}\n\n"
        f"Slack から {project_name} への依頼を Claude Code が実施したもの。\n\n"
        f"依頼内容:\n{prompt.strip()}\n\n"
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
    )
