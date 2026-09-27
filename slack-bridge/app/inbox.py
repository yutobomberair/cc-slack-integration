"""Slack に添付されたファイルをローカルへ降ろす。

スマホで撮った資料や受け取った CSV をそのままメンションに添付して
「これを元に整理して」と頼めるようにするための経路。

保存先はプロジェクト直下の ``_inbox/``。プロジェクトのファイルに直接混ぜないのは、
外から来たものと自分で作ったものを区別できなくなるため。Claude には保存先の相対パスを
プロンプトで伝えるので、``_inbox/`` の中を読ませればよい。

``url_private`` の取得には bot トークンによる Bearer 認証が必要で、Slack App に
``files:read`` スコープが要る。スコープが無いと HTML のログインページが返ってくる
（403 ではない）ため、Content-Type を見て弾いている。

注意: ここで降ろしたファイルは、Claude が Bash で読める場所に置かれる。Slack へ
ファイルを投げられる人は、このPCで実行される内容に影響を与えられるということ。
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.paths import relative_prefix, unique_path

logger = logging.getLogger(__name__)

#: 保存先ディレクトリ名（プロジェクトの working_directory 直下）
INBOX_DIR = "_inbox"

#: これを超えるファイルは降ろさない
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

#: ダウンロードのタイムアウト（秒）
_TIMEOUT = 60

#: ファイル名に使えない文字。Windows を基準にする。
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class Saved:
    path: str       # プロジェクトルートからの相対パス
    name: str       # Slack 上の元のファイル名
    size: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def safe_name(name: str) -> str:
    """Slack のファイル名をそのままファイルシステムに書けるようにする。

    パス区切りを含む名前で ``_inbox`` の外へ出られないようにするのが主目的。
    """
    cleaned = _UNSAFE_RE.sub("_", (name or "").strip()) or "file"
    # ".." や先頭ドットだけの名前を潰す
    cleaned = cleaned.strip(". ") or "file"
    return cleaned[:150]


def _download(url: str, token: str) -> bytes:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            # スコープ不足だと Slack はエラーではなくログインページを返す。
            raise PermissionError(
                "Slack App に `files:read` スコープがありません。\n"
                "https://api.slack.com/apps → OAuth & Permissions → Bot Token Scopes に "
                "`files:read` を追加し、Reinstall to Workspace を実行してください。"
            )
        return response.read(MAX_DOWNLOAD_BYTES + 1)


def save_files(files: list[dict], cwd: Path, token: str) -> list[Saved]:
    """イベントに含まれる添付ファイルを ``_inbox/`` へ降ろす。

    1件ずつ独立して扱う。1つ落とせなくても他は保存する。
    """
    if not files:
        return []

    directory = Path(cwd) / INBOX_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.exception("保存先を作成できませんでした: %s", directory)
        return [
            Saved(path="", name=f.get("name", "?"), size=0, error=str(exc)) for f in files
        ]

    saved: list[Saved] = []
    for meta in files:
        name = meta.get("name") or meta.get("title") or "file"
        declared = int(meta.get("size") or 0)
        url = meta.get("url_private_download") or meta.get("url_private")

        if not url:
            # 外部連携のファイル（Google Drive など）は実体を持たない
            saved.append(Saved(path="", name=name, size=declared, error="ダウンロードURLがありません"))
            continue
        if declared > MAX_DOWNLOAD_BYTES:
            saved.append(Saved(path="", name=name, size=declared, error="サイズが上限を超えています"))
            continue

        try:
            payload = _download(url, token)
        except PermissionError as exc:
            # スコープ不足はこの先も全部失敗する。理由を伝えて残りは試さない。
            saved.append(Saved(path="", name=name, size=declared, error=str(exc)))
            return saved
        except (urllib.error.URLError, OSError) as exc:
            logger.exception("ファイルの取得に失敗しました name=%s", name)
            saved.append(Saved(path="", name=name, size=declared, error=str(exc)))
            continue

        if len(payload) > MAX_DOWNLOAD_BYTES:
            saved.append(Saved(path="", name=name, size=len(payload), error="サイズが上限を超えています"))
            continue

        try:
            target = unique_path(directory / safe_name(name))
            target.write_bytes(payload)
        except OSError as exc:
            logger.exception("ファイルの保存に失敗しました name=%s", name)
            saved.append(Saved(path="", name=name, size=len(payload), error=str(exc)))
            continue

        relative = target.relative_to(Path(cwd)).as_posix()
        logger.info("添付ファイルを保存しました %s (%d bytes)", relative, len(payload))
        saved.append(Saved(path=relative, name=name, size=len(payload)))

    return saved


def prompt_note(saved: list[Saved], workdir_label: str = "") -> str:
    """Claude へ「どこに置いたか」を伝える一文。

    保存しただけでは Claude はファイルの存在を知らないので、依頼文の前に付ける。

    ``workdir_label`` は Claude が動く階層（プロジェクト直下からの相対表記）。
    ``_inbox/`` はプロジェクト直下にあるので、下の階層で動いているときは
    そこから見た相対パス（``../_inbox/...``）で伝える。
    """
    ok = [s for s in saved if s.ok]
    if not ok:
        return ""
    prefix = relative_prefix(workdir_label)
    lines = ["[Slack に添付されたファイルを次の場所へ保存しました]"]
    lines.extend(f"- {prefix}{s.path}" for s in ok)
    return "\n".join(lines) + "\n\n"


def report(saved: list[Saved]) -> str:
    """Slack へ返す保存結果。"""
    if not saved:
        return ""
    ok = [s for s in saved if s.ok]
    failed = [s for s in saved if not s.ok]
    lines = []
    if ok:
        lines.append(f":inbox_tray: 添付ファイル {len(ok)} 件を `{INBOX_DIR}/` に保存しました。")
        lines.extend(f"`{s.path}`" for s in ok)
    for item in failed:
        lines.append(f":warning: `{item.name}` を保存できませんでした（{item.error}）。")
    return "\n".join(lines)
