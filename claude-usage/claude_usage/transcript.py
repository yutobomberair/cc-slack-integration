"""プロジェクト別・モデル別のトークン集計（仕様書 §7）。

``~/.claude/projects/**/*.jsonl`` を読んで、``cwd`` ごとにトークン数を足す。

## これは Quota の配分ではない

仕様書 §7 が明示しているとおり、**Subscription Quota はプロジェクト単位で提供されて
いない**。だからここで出すのは「トークン数の比率」であって、「5時間枠の何%をどの
プロジェクトが使ったか」ではない。前者から後者を勝手に作って公式値のように見せない
（§20）。

実用上の意味は「どのプロジェクトが重いか」の当たりを付けること。枠が尽きそうなときに
どこを絞るか決める材料になる。

## 比率の出し方

トークンには4種類あり、桁が大きく違う（実測）。

    cache_read       45億      … 既に作ったキャッシュの読み直し
    cache_creation    2億      … キャッシュの作成
    output         1,860万
    input           2.6万

``cache_read`` を含めると、比率が「セッションの長さ」に支配されて作業量を表さなく
なる。そこで**比率は input + output + cache_creation で出し、``cache_read`` は
内訳として別に表示する**。どちらで出しても順位はほぼ同じだった（21.0% と 24.2%）が、
どの式で出しているかを黙っておくべきではない。

## 集計の単位

``cwd`` でそのまま分けると ``propose`` と ``propose/movie`` が別扱いになる
（``cd:`` で階層を移れるので実際に起きる）。git リポジトリのルートまで遡って
まとめる方法も用意する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Claude Code が transcript を置く場所。
def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    messages: int = 0

    #: 比率に使う量。cache_read を含めない（docstring 参照）。
    @property
    def weighted(self) -> int:
        return self.input + self.output + self.cache_creation

    @property
    def total(self) -> int:
        return self.weighted + self.cache_read

    def add(self, other: "Tokens") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_creation += other.cache_creation
        self.cache_read += other.cache_read
        self.messages += other.messages


@dataclass
class Aggregate:
    """集計の結果。"""

    by_path: dict[str, Tokens] = field(default_factory=dict)
    by_model: dict[str, Tokens] = field(default_factory=dict)
    total: Tokens = field(default_factory=Tokens)
    #: 読んだファイル数と、日付で除外したレコード数。数が合わないときの手掛かり。
    files: int = 0
    skipped_outside_range: int = 0
    since: datetime | None = None


def _usage_of(record: dict) -> Tokens | None:
    """assistant のレコードから usage を取り出す。それ以外は None。"""
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    def count(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return Tokens(
        input=count("input_tokens"),
        output=count("output_tokens"),
        cache_creation=count("cache_creation_input_tokens"),
        cache_read=count("cache_read_input_tokens"),
        messages=1,
    )


def _timestamp_of(record: dict) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        # transcript は末尾 Z の UTC
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def repo_root_of(path: str, cache: dict[str, str] | None = None) -> str:
    """``path`` を含む git リポジトリのルート。見つからなければ ``path`` のまま。

    ``propose`` と ``propose/movie`` を1つにまとめるために使う（``cd:`` で階層を
    移れるので実際に分かれる）。git を起動せずに ``.git`` を遡って探す。
    存在しないパスはそのまま返す（過去の記録に残っているだけの場合がある）。
    """
    if cache is not None and path in cache:
        return cache[path]
    result = path
    try:
        current = Path(path)
        if current.exists():
            for candidate in [current, *current.parents]:
                if (candidate / ".git").exists():
                    result = str(candidate)
                    break
    except OSError:
        pass
    if cache is not None:
        cache[path] = result
    return result


def aggregate(
    since: datetime | None = None,
    root: Path | None = None,
    by_repo: bool = True,
) -> Aggregate:
    """transcript を読んで集計する。

    ``since`` より古いレコードは数えない。既定は全期間。実測で 63ファイル
    12,718レコードが 0.64秒だったので、毎回読んでも問題にならない（キャッシュを
    持つと古い値を出す危険が増えるだけ）。
    """
    directory = root or projects_dir()
    out = Aggregate(since=since)
    repo_cache: dict[str, str] = {}

    try:
        files = sorted(directory.rglob("*.jsonl"))
    except OSError:
        return out

    for path in files:
        out.files += 1
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    # 大半のレコードは usage を持たない。JSON を組む前に弾く
                    if '"usage"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue

                    tokens = _usage_of(record)
                    if tokens is None:
                        continue

                    if since is not None:
                        moment = _timestamp_of(record)
                        if moment is None or moment < since:
                            out.skipped_outside_range += 1
                            continue

                    key = record.get("cwd") or "(不明)"
                    if by_repo:
                        key = repo_root_of(key, repo_cache)
                    out.by_path.setdefault(key, Tokens()).add(tokens)

                    model = (record.get("message") or {}).get("model") or "(不明)"
                    out.by_model.setdefault(model, Tokens()).add(tokens)

                    out.total.add(tokens)
        except OSError:
            continue

    return out


def label_for(path: str) -> str:
    """表示用の短い名前。末尾の1〜2階層を使う。"""
    if path == "(不明)":
        return path
    parts = [p for p in Path(path).parts if p not in ("\\", "/")]
    if len(parts) <= 1:
        return path
    name = parts[-1]
    # ドライブ直下や .claude のような区別の付かない名前は親を添える
    if name.startswith(".") and len(parts) >= 2:
        return f"{parts[-2]}/{name}"
    return name


def ranked(result: Aggregate, limit: int = 10) -> list[tuple[str, Tokens, float]]:
    """(パス, トークン, 比率%) を重い順に。比率は ``weighted`` で出す。"""
    grand = result.total.weighted
    rows = sorted(result.by_path.items(), key=lambda kv: -kv[1].weighted)
    return [
        (path, tokens, (tokens.weighted / grand * 100.0) if grand else 0.0)
        for path, tokens in rows[:limit]
    ]


def ranked_models(result: Aggregate, limit: int = 6) -> list[tuple[str, Tokens, float]]:
    grand = result.total.weighted
    rows = sorted(result.by_model.items(), key=lambda kv: -kv[1].weighted)
    return [
        (name, tokens, (tokens.weighted / grand * 100.0) if grand else 0.0)
        for name, tokens in rows[:limit]
    ]


def window_start(resets_at: int | None, days: int = 7) -> datetime | None:
    """7日枠の開始時刻。``resets_at`` から遡る。

    「この枠でどのプロジェクトが消費したか」が見たい情報なので、暦の7日ではなく
    実際の枠に合わせる。
    """
    if resets_at is None:
        return None
    return datetime.fromtimestamp(resets_at, tz=timezone.utc) - timedelta(days=days)


def human(count: int) -> str:
    """トークン数を読める形に。"""
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if count >= size:
            return f"{count / size:.1f}{unit}"
    return str(count)
