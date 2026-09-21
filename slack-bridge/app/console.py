"""Windows コンソールの UTF-8 化。

Windows の標準出力は既定で cp932 のため、日本語混じりのログや調査結果をそのまま
print / logging すると UnicodeEncodeError で落ちる（em dash のような文字でも起こる）。
プロセス起動時に一度だけ呼ぶ。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
