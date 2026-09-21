"""依頼文から実行モードを判定する（CLAUDE.md §20）。

コード変更は「二重の鍵」で守る。

1. プロジェクト側で ``allow_implement: true`` が設定されていること
2. 依頼文の先頭に昇格キーワードが書かれていること

既定は常に調査モード。スマホから誤ってコード変更が走ることを防ぐため、
書き込みは毎回明示的に要求させる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 依頼文の先頭に置くキーワード。行頭のみを見る（本文中の「実装:」に反応しないため）。
#
# 選択肢は必ず長いものから並べる。正規表現の選択は左から順に最初に一致したものを
# 採るため、"/impl" を先に置くと "/implement" が "/impl" + "ement" と解釈され、
# 残った "ement" がプロンプトに混入する。
_IMPLEMENT_RE = re.compile(
    r"^\s*(?:/implement|/impl|implement|impl|実装)\s*[:：]?\s*\n?", re.IGNORECASE
)

INVESTIGATE = "investigate"
IMPLEMENT = "implement"


@dataclass(frozen=True)
class TaskMode:
    profile_key: str
    prompt: str
    #: 昇格を要求されたが、プロジェクト設定で許可されていない
    denied: bool = False

    @property
    def is_implement(self) -> bool:
        return self.profile_key == IMPLEMENT


def resolve(prompt: str, project_profile: str, allow_implement: bool) -> TaskMode:
    """依頼文を解析し、使用するプロファイルとキーワード除去後の本文を返す。"""
    match = _IMPLEMENT_RE.match(prompt or "")
    if not match:
        return TaskMode(profile_key=project_profile, prompt=prompt)

    body = prompt[match.end():].strip()
    if not allow_implement:
        # キーワードは剥がさずに返す。拒否理由を Slack へ伝えるだけで実行はしない。
        return TaskMode(profile_key=project_profile, prompt=prompt, denied=True)

    return TaskMode(profile_key=IMPLEMENT, prompt=body)


def implement_not_allowed_message(project_name: str) -> str:
    return (
        ":lock: このプロジェクトではコード変更が許可されていません。\n"
        f"Project: *{project_name}*\n\n"
        "`config/projects.yaml` の該当プロジェクトに `allow_implement: true` を設定し、"
        "Slack Bridge を再起動してください。"
    )


def not_a_repo_message(project_name: str) -> str:
    return (
        ":lock: このプロジェクトは git 管理されていないため、コード変更を実行しません。\n"
        f"Project: *{project_name}*\n\n"
        "変更を戻す手段が無い状態で書き込むのは危険です。"
        "対象ディレクトリで `git init` してから再度依頼してください。"
    )
