"""出力先の文字コードに合わせて表示を落とす（仕様書 §14 の環境依存 Adapter）。

Windows の既定コンソールは cp932 で、``█`` や ``↺`` を encode できない。素朴に
``print`` すると ``UnicodeEncodeError`` で**何も表示されないまま終わる**。実際に
``↺`` で踏んだ。statusline が黙って空行を返すと、利用者は「利用枠が取れていない」と
誤解するので、文字が出せるかを先に確かめて代替表記へ落とす。

UTF-8 を強制しない理由は、本当に cp932 の端末へ UTF-8 のバイト列を送ると文字化けし、
「壊れた表示」に変わるだけで解決しないため。**出せるものだけ出す**方針を採る。
"""

from __future__ import annotations

import sys


def _encoding() -> str:
    return (getattr(sys.stdout, "encoding", None) or "ascii").lower()


def supports(text: str) -> bool:
    """``text`` を出力先の文字コードで書けるか。"""
    try:
        text.encode(_encoding())
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def pick(preferred: str, fallback: str) -> str:
    """出せるなら ``preferred``、駄目なら ``fallback``。"""
    return preferred if supports(preferred) else fallback


#: リセット時刻の目印。cp932 では ASCII に落ちる。
RESET_MARK = None


def reset_mark() -> str:
    global RESET_MARK
    if RESET_MARK is None:
        RESET_MARK = pick("↺", "reset")
    return RESET_MARK


def bar(percent: float | None, width: int = 10) -> str:
    """プログレスバー（仕様書 §8, §22）。

    ブロック文字が出せない環境では ``#`` と ``-`` に落とす。値が無いときは
    埋めずに ``?`` を並べる（0% と区別する）。
    """
    filled_char = pick("█", "#")     # █
    empty_char = pick("░", "-")      # ░
    if percent is None:
        return pick("░", "?") * width
    ratio = max(0.0, min(100.0, percent)) / 100.0
    filled = int(round(ratio * width))
    return filled_char * filled + empty_char * (width - filled)


def write_line(text: str) -> None:
    """1行出力する。encode できない文字が残っていても落とさない。

    ここまでで代替表記に落としているはずだが、モデル名やプロジェクト名のように
    こちらが選んでいない文字列も混ざるので、最後の砦を置く。
    """
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        encoding = _encoding()
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding) + "\n")
