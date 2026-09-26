"""ファイル配置の小さな共通処理。

受信（``app/inbox.py``）と送信後の退避（``app/artifacts.py``）の両方で、
「既にあるものを潰さずに置く」必要がある。同じ規則で振る舞ってほしいので
1か所にまとめる。
"""

from __future__ import annotations

from pathlib import Path


def unique_path(target: Path) -> Path:
    """``target`` が空いていればそのまま、埋まっていれば ``名前-1.拡張子`` を返す。

    黙って上書きしない。受信側では同じファイルを送り直したときに前のものが消え、
    送信後の退避では同名の成果物を出すたびに履歴が消えてしまうため。
    """
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(1, 1000):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"{target.name} の置き場所を確保できませんでした")
