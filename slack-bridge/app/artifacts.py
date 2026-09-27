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
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger(__name__)

#: 走査から外すディレクトリ名。git 管理外のプロジェクトでのみ使う。
_SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
    ".idea", ".vscode", ".claude",
    "logs", "state",
    # _share/ は送信の待ち行列（app/share_queue.py）。通常の出力とは別に扱う。
    "_share",
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

    def record(
        self,
        channel_id: str,
        thread_ts: str,
        project_key: str,
        paths: list[str],
        base: str | None = None,
    ) -> None:
        """一覧を記録する。

        ``base`` はパスを解決する基準。git 由来のパスはリポジトリルート基準で、
        それはプロジェクト直下と一致しないことがある。あとで ``共有: 1`` を
        解決するときに同じ基準を使わないとファイルを見失う。
        """
        with self._lock:
            self._data = self._load()  # 別インスタンスの記録を踏み潰さない
            self._data[self._key(channel_id, thread_ts)] = {
                "project": project_key,
                "paths": paths[:MAX_LISTED],
                "base": base,
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

    def get_base(self, channel_id: str, thread_ts: str) -> str | None:
        """記録時のパス解決基準。古い記録には無いので None を返しうる。"""
        with self._lock:
            self._data = self._load()
            entry = self._data.get(self._key(channel_id, thread_ts))
            return entry.get("base") if entry else None
