"""実行で出力されたファイルの一覧と、Slack への受け渡し。

Slack から依頼した場合、生成物は自宅PCの中にあるのでスマホからは見えない。
そこで二段構えにする。

1. 実行後、この実行で出力されたファイルを一覧でスレッドへ出す（自動）
2. 利用者が ``共有: 1`` と書いたものだけをアップロードする

毎回すべて添付しない理由は、実装タスクではソースが十数ファイル変わることがあり、
それが全部スマホへ流れてくるとスレッドが埋まって肝心の回答が読めなくなるため。
一覧だけなら数行で済み、欲しいものは後から番号で取り出せる。

例外が ``_share/``。Claude がここへ置いたファイルは「共有したい」という意思表示
とみなし、一覧を挟まず自動で添付する。``_share/`` は送信の待ち行列として扱い、
**そこにあるものは送る**。送れたら ``_share/sent/`` へ退かす。したがって

* 送信に失敗したファイルは ``_share/`` に残る → 次の実行で自動的に再試行される
* 送信できたファイルは居なくなる → 二度と重複して届かない
* 状態はディレクトリを見れば分かる（未送信か送信済みか）

削除ではなく移動にしているのは、Claude が cp ではなく mv で持ってきた場合に
PC上の実体を失わないため。Claude 自身に Slack トークンを触らせずに
共有させるための出口で、置くだけで届くので特別な API も手順も要らない。
``_share/`` は転送用の置き場であってプロジェクトの成果物ではないため、自動コミットと
通常の一覧からは外す。

出力ファイルの検出は2通り。

* git 管理下 … ``git status --porcelain`` の差分（``app/git_ops.py``）。
  .gitignore が効くので、ビルド生成物や venv を拾わない。
* git 管理外 … ここで mtime とサイズを走査して比較する。除外は自前で持つ。
"""

from __future__ import annotations

import json
import logging
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.paths import unique_path

logger = logging.getLogger(__name__)

#: Claude が「Slack へ送りたい」ものを置くディレクトリ（プロジェクト直下）。
SHARE_DIR = "_share"

#: 送信できたファイルの退避先（``_share/`` の直下）。走査対象からは外す。
SENT_DIR = "sent"

#: 走査から外すディレクトリ名。git 管理外のプロジェクトでのみ使う。
_SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
    ".idea", ".vscode", ".claude",
    "logs", "state",
    SHARE_DIR,
}

#: 走査の上限。巨大なディレクトリで固まらないための保険。
_MAX_SCAN_ENTRIES = 50_000

#: 一覧に載せる最大件数。これを超えた分は件数だけ知らせる。
MAX_LISTED = 30

#: アップロードを諦めるサイズ。Slack の上限より手前で切る。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


# --------------------------------------------------------------------------
# git 管理外のプロジェクト向けの差分検出
# --------------------------------------------------------------------------

def scan(cwd: Path) -> dict[str, tuple[int, int]]:
    """相対パス -> (mtime_ns, size) を返す。

    git 管理外のプロジェクトで「この実行で何が出力されたか」を出すために使う。
    内容のハッシュは取らない。生成物の検出が目的なので mtime とサイズで足りるし、
    大きなディレクトリを毎回ハッシュすると実行のたびに待たされる。
    """
    found: dict[str, tuple[int, int]] = {}
    root = Path(cwd)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if len(found) >= _MAX_SCAN_ENTRIES:
                logger.warning("走査が上限に達しました。一部のファイルは検出されません: %s", root)
                return found
            name = entry.name
            try:
                if entry.is_dir():
                    if name not in _SKIP_DIRS:
                        stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            found[entry.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return found


def changed_since(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    """``scan`` の結果2つを比べ、作成または変更されたパスを返す。"""
    return sorted(p for p, meta in after.items() if before.get(p) != meta)


# --------------------------------------------------------------------------
# _share/ — Claude からの明示的な共有
# --------------------------------------------------------------------------

def list_share_files(cwd: Path) -> list[str]:
    """``_share/`` にある未送信ファイルの相対パス。``_share/sent/`` は含めない。

    git 管理の有無に関わらず常にこちらで見る。``.gitignore`` に ``_share/`` が
    書かれていると git の差分には出てこないが、それでも共有はできてほしいため。

    内容の比較はしない。``_share/`` は送信待ちの置き場で、送れたものは
    ``archive_sent`` が退かすので、「そこにある = まだ送っていない」と等しい。
    """
    directory = Path(cwd) / SHARE_DIR
    if not directory.is_dir():
        return []
    sent_dir = directory / SENT_DIR
    found: list[str] = []
    for entry in directory.rglob("*"):
        try:
            if not entry.is_file():
                continue
            if sent_dir in entry.parents:
                continue
        except OSError:
            continue
        found.append(entry.relative_to(Path(cwd)).as_posix())
    return sorted(found)


def archive_sent(cwd: Path, paths: list[str], retention_days: int = 7) -> list[str]:
    """送信できたファイルを待ち行列から外す。返り値は外せなかったパス。

    ``retention_days`` が正なら ``_share/sent/`` へ移し、その日数を過ぎた退避分を
    掃除する。0 以下なら移さずに削除する。

    既定で少し残すのは、``_share/`` に直接生成されたファイルはコピー元が無く、
    即削除するとPC上から実体が消えるため。送信に成功していれば Slack 側には
    残っているので致命的ではないが、気づいて拾い直す猶予があるほうが安全。

    移動・削除に失敗しても送信自体は済んでいるので例外にはしない。ただし次の実行で
    もう一度届いてしまうため、呼び出し側が警告を出せるよう結果を返す。
    """
    root = Path(cwd)
    failed: list[str] = []
    for rel in paths:
        source = root / rel
        try:
            if retention_days <= 0:
                source.unlink()
                continue
            # _share/nested/a.md -> _share/sent/nested/a.md（階層を保つ）
            inner = Path(rel).relative_to(SHARE_DIR)
            destination = root / SHARE_DIR / SENT_DIR / inner
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(unique_path(destination))
        except OSError:
            logger.exception("送信済みファイルを退避できませんでした: %s", rel)
            failed.append(rel)

    if retention_days > 0:
        prune_sent(cwd, retention_days)
    return failed


def prune_sent(cwd: Path, retention_days: int) -> int:
    """``_share/sent/`` のうち保持期間を過ぎたものを消す。返り値は消した件数。

    ここを掃除しないと退避先が延々と膨らむ。送信済みのものしか入っていないので、
    期間を過ぎたら消してよい（Slack 側には残っている）。
    """
    directory = Path(cwd) / SHARE_DIR / SENT_DIR
    if not directory.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for entry in directory.rglob("*"):
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            logger.debug("退避分を削除できませんでした: %s", entry, exc_info=True)
    if removed:
        logger.info("%s/%s から %d 件を削除しました（保持 %d 日）",
                    SHARE_DIR, SENT_DIR, removed, retention_days)
    return removed


def is_shared_path(rel: str) -> bool:
    return rel == SHARE_DIR or rel.startswith(SHARE_DIR + "/")


def split_shared(paths: list[str]) -> tuple[list[str], list[str]]:
    """(``_share/`` のもの, それ以外) に分ける。"""
    shared = [p for p in paths if is_shared_path(p)]
    rest = [p for p in paths if not is_shared_path(p)]
    return shared, rest


def auto_share_report(
    sent: list[str], items: list[Artifact], stuck: list[str] | None = None
) -> str:
    """``_share/`` から自動添付したときの報告。

    ``stuck`` は送信はできたが ``_share/sent/`` へ退避できなかったもの。放っておくと
    次の実行でもう一度届くので、黙って重複させずに知らせる。
    """
    if not items:
        return ""
    lines = []
    if sent:
        lines.append(f":outbox_tray: `{SHARE_DIR}/` のファイル {len(sent)} 件を添付しました。")
    for item in items:
        if item.missing or item.too_large:
            reason = "削除済み" if item.missing else f"{human_size(item.size)} は大きすぎます"
            lines.append(f":warning: `{item.path}` は送れませんでした（{reason}）。")
    skipped = sum(1 for i in items if i.missing or i.too_large)
    unsent = len(items) - len(sent) - skipped
    if unsent > 0:
        lines.append(
            f":warning: {unsent} 件を送れませんでした。"
            f"`{SHARE_DIR}/` に残してあるので、次の実行で再試行します。"
        )
    for path in stuck or []:
        lines.append(
            f":warning: `{path}` を `{SHARE_DIR}/{SENT_DIR}/` へ退避できませんでした。"
            "このままだと次の実行でもう一度届きます。"
        )
    return "\n".join(lines)


def share_dir_note() -> str:
    """Claude へ ``_share/`` の使い方を伝える一文。

    伝えないと存在を知りようがないので、実装モードの依頼文の前に付ける。
    """
    return (
        "[この依頼は Slack 経由です。"
        f"利用者に渡したいファイルは `{SHARE_DIR}/` へ置けば、"
        "このスレッドへ自動で添付されます]\n\n"
    )


# --------------------------------------------------------------------------
# 一覧の組み立て
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    path: str          # プロジェクトルートからの相対パス
    size: int          # バイト。取得できなければ -1
    missing: bool = False

    @property
    def too_large(self) -> bool:
        return self.size > MAX_UPLOAD_BYTES


def describe(cwd: Path, paths: list[str]) -> list[Artifact]:
    """相対パスの一覧に、現在のサイズを付けて返す。"""
    items: list[Artifact] = []
    for rel in paths:
        target = Path(cwd) / rel
        try:
            items.append(Artifact(path=rel, size=target.stat().st_size))
        except OSError:
            # 実行中に作られて消えた場合など。一覧には残すが取得できない旨を示す。
            items.append(Artifact(path=rel, size=-1, missing=True))
    return items


def human_size(size: int) -> str:
    if size < 0:
        return "不明"
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


def listing_message(items: list[Artifact]) -> str:
    """出力ファイルの一覧。番号は ``共有: N`` で指定するためのもの。"""
    if not items:
        return ""
    shown = items[:MAX_LISTED]
    lines = [":paperclip: *この実行で出力されたファイル*"]
    for index, item in enumerate(shown, start=1):
        note = ""
        if item.missing:
            note = " — 取得できません（削除済み）"
        elif item.too_large:
            note = f" — 大きすぎるため共有できません（上限 {human_size(MAX_UPLOAD_BYTES)}）"
        lines.append(f"`{index}` `{item.path}` （{human_size(item.size)}）{note}")
    if len(items) > len(shown):
        lines.append(f"…ほか {len(items) - len(shown)} 件")
    lines.append("")
    lines.append("受け取るには `共有: 1` のように番号を指定してください（`共有: all` で全部）。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 共有ディレクティブの解釈
# --------------------------------------------------------------------------

_SHARE_TOKENS = ("共有", "share", "/share", "送って", "ちょうだい")
_ALL_TOKENS = {"all", "*", "全部", "すべて", "全て"}


def parse_share_request(prompt: str) -> str | None:
    """``共有: 1,3`` のような依頼なら、コロンより後ろを返す。そうでなければ None。

    先頭行だけを見る。本文中の「共有:」に反応すると、共有の仕組みそのものを
    相談したいときに Claude へ渡らなくなる。
    """
    text = (prompt or "").strip()
    head, _, rest = text.partition("\n")
    for separator in (":", "："):
        key, sep, value = head.partition(separator)
        if not sep:
            continue
        if key.strip().lower() in _SHARE_TOKENS:
            return (value + ("\n" + rest if rest else "")).strip()
    return None


def select(items: list[Artifact], argument: str) -> tuple[list[Artifact], list[str]]:
    """``共有:`` の引数から対象を選ぶ。返り値は (選ばれたもの, 解決できなかった語)。

    番号・``all``・ファイル名の部分一致を受け付ける。スマホからの入力なので、
    パスを正確に打たせずに済むことを優先している。
    """
    tokens = [t for t in _split_arguments(argument) if t]
    if not tokens or any(t.lower() in _ALL_TOKENS for t in tokens):
        return list(items), []

    chosen: list[Artifact] = []
    unresolved: list[str] = []
    for token in tokens:
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(items):
                chosen.append(items[index - 1])
            else:
                unresolved.append(token)
            continue
        needle = token.lower().strip("`'\"")
        matched = [i for i in items if needle in i.path.lower()]
        if matched:
            chosen.extend(matched)
        else:
            unresolved.append(token)

    # 同じファイルを2回指定されても1回だけ送る（順序は維持する）
    deduped: list[Artifact] = []
    seen: set[str] = set()
    for item in chosen:
        if item.path not in seen:
            seen.add(item.path)
            deduped.append(item)
    return deduped, unresolved


def _split_arguments(argument: str) -> list[str]:
    out: list[str] = []
    for part in (argument or "").replace("、", ",").replace("，", ",").split(","):
        out.extend(p for p in part.split() if p)
    return out


def no_artifacts_message() -> str:
    return (
        ":open_file_folder: このスレッドには共有できるファイルの記録がありません。\n\n"
        "ファイルを出力する依頼を実行すると一覧が投稿され、その番号で受け取れます。"
    )


def unresolved_message(unresolved: list[str], items: list[Artifact]) -> str:
    listed = "\n".join(
        f"`{i}` `{item.path}`" for i, item in enumerate(items[:MAX_LISTED], start=1)
    )
    return (
        f":thinking_face: `{'`, `'.join(unresolved)}` に一致するファイルがありません。\n\n"
        f"{listed}"
    )


# --------------------------------------------------------------------------
# アップロード
# --------------------------------------------------------------------------

class UploadError(RuntimeError):
    """Slack へのアップロードに失敗した。"""


def upload(client, channel_id: str, thread_ts: str, cwd: Path, items: list[Artifact]) -> list[str]:
    """選ばれたファイルをスレッドへ添付する。返り値は送れたパス。

    1件ずつ送る。まとめて送ると1件の失敗で全部が落ちるため、部分的にでも
    届いたほうが実用的。
    """
    sent: list[str] = []
    for item in items:
        if item.missing or item.too_large:
            continue
        target = Path(cwd) / item.path
        try:
            client.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                file=str(target),
                filename=Path(item.path).name,
                title=item.path,
            )
            sent.append(item.path)
        except Exception as exc:  # noqa: BLE001 - SlackApiError 以外も来うる
            if "missing_scope" in str(exc):
                # スコープ不足はこのまま何度でも失敗する。原因を明示して打ち切る。
                raise UploadError(
                    "Slack App に `files:write` スコープがありません。\n"
                    "https://api.slack.com/apps → OAuth & Permissions → Bot Token Scopes に "
                    "`files:write` を追加し、Reinstall to Workspace を実行してください。"
                ) from exc
            logger.exception("ファイルのアップロードに失敗しました path=%s", item.path)
    return sent


def upload_report(sent: list[str], items: list[Artifact]) -> str:
    skipped = [i for i in items if i.missing or i.too_large]
    if not sent and not skipped:
        return ":open_file_folder: 共有できるファイルがありませんでした。"
    lines = []
    if sent:
        lines.append(f":outbox_tray: {len(sent)} 件を共有しました。")
    for item in skipped:
        reason = "削除済み" if item.missing else f"{human_size(item.size)} は大きすぎます"
        lines.append(f":warning: `{item.path}` は送れませんでした（{reason}）。")
    failed = len(items) - len(skipped) - len(sent)
    if failed > 0:
        lines.append(f":warning: {failed} 件はアップロードに失敗しました。ログを確認してください。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# スレッドごとの記録
# --------------------------------------------------------------------------

class ArtifactStore:
    """スレッド -> 直近の出力ファイル一覧。

    ``共有: 1`` の「1」を解決するために、一覧を出したときの順序を覚えておく必要がある。
    ブリッジを再起動しても番号が生き続けるようファイルへ落とす（``SessionStore`` と同じ方針）。
    """

    def __init__(self, path: Path, max_entries: int = 200) -> None:
        self._path = path
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("出力ファイルの記録を読めませんでした。空から開始します: %s", self._path)
            return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except OSError:
            logger.exception("出力ファイルの記録の保存に失敗しました: %s", self._path)

    @staticmethod
    def _key(channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def record(self, channel_id: str, thread_ts: str, project_key: str, paths: list[str]) -> None:
        with self._lock:
            self._data = self._load()  # 別インスタンスの記録を踏み潰さない
            self._data[self._key(channel_id, thread_ts)] = {
                "project": project_key,
                "paths": paths[:MAX_LISTED],
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if len(self._data) > self._max_entries:
                ordered = sorted(self._data.items(), key=lambda kv: kv[1].get("updated_at", ""))
                for key, _ in ordered[: len(self._data) - self._max_entries]:
                    del self._data[key]
            self._save()

    def get(self, channel_id: str, thread_ts: str) -> list[str]:
        with self._lock:
            self._data = self._load()
            entry = self._data.get(self._key(channel_id, thread_ts))
            return list(entry.get("paths", [])) if entry else []
