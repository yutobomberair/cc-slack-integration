"""依頼文の先頭ディレクティブを解釈する（CLAUDE.md §20）。

依頼文の先頭には、コロンで終わる短い指示区間を置ける。

    README を直して                    → ディレクティブ無し（プロジェクト既定＝実装）
    調査: 構成を教えて                  → 読み取り専用に降格
    opus: 設計を詰めて                 → プロジェクト既定のまま、Opus
    調査 haiku: ざっと見て              → 読み取り専用、Haiku

トークンは空白かカンマ区切りで、順序は問わない。既知のトークンが1つも無い区間は
ディレクティブとして扱わないので、``TODO: 〜`` や ``URL: 〜`` のような普通の文が
誤って解釈されることはない。

既定は ``config/projects.yaml`` の ``profile``（現在はどのプロジェクトも
``implement``）。PC で claude を起動したときと同じ権限で動かすための設定で、
Slack から書き込みもシェルも使える。

そのうえで、読み取りだけで走らせたいときは先頭に ``調査:`` と書いて降格できる。
``allow_implement: false`` のプロジェクトでは昇格キーワードを拒否する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INVESTIGATE = "investigate"
IMPLEMENT = "implement"

# 実装モードへの昇格キーワード
_IMPLEMENT_TOKENS = {"実装", "impl", "implement", "/impl", "/implement"}

# 読み取り専用への降格キーワード。既定が implement になったので、
# 「今回は触らずに調べるだけ」を明示する手段としてこちらを用意する。
_INVESTIGATE_TOKENS = {"調査", "investigate", "read", "/read", "/investigate"}

# 先頭のディレクティブ区間。コロンまでを候補として切り出す。
# 長すぎる区間は普通の文とみなすため、40文字で打ち切る。
_DIRECTIVE_RE = re.compile(r"^[ \t]*([^\n:：]{1,40})[:：][ \t]*\n?")
_SPLIT_RE = re.compile(r"[\s,、]+")


@dataclass(frozen=True)
class TaskMode:
    profile_key: str
    prompt: str
    #: 依頼文で指定されたモデル（未指定なら None = 設定の既定モデル）
    model: str | None = None
    #: 昇格を要求されたが、プロジェクト設定で許可されていない
    denied: bool = False
    #: 未知のモデル名が指定された
    unknown_model: str | None = None

    @property
    def is_implement(self) -> bool:
        return self.profile_key == IMPLEMENT


def resolve(
    prompt: str,
    project_profile: str,
    allow_implement: bool,
    allowed_models: list[str] | None = None,
) -> TaskMode:
    """依頼文を解析し、プロファイル・モデル・本文を返す。"""
    allowed = {m.lower() for m in (allowed_models or [])}
    text = prompt or ""

    match = _DIRECTIVE_RE.match(text)
    if not match:
        return TaskMode(profile_key=project_profile, prompt=text.strip())

    tokens = [t.lower() for t in _SPLIT_RE.split(match.group(1).strip()) if t]
    wants_implement = any(t in _IMPLEMENT_TOKENS for t in tokens)
    wants_investigate = any(t in _INVESTIGATE_TOKENS for t in tokens)
    models = [t for t in tokens if t in allowed]
    unknown = [
        t
        for t in tokens
        if t not in _IMPLEMENT_TOKENS
        and t not in _INVESTIGATE_TOKENS
        and t not in allowed
    ]

    # 既知のトークンが1つも無ければ、ただの文章。ディレクティブとして扱わない。
    if not wants_implement and not wants_investigate and not models:
        return TaskMode(profile_key=project_profile, prompt=text.strip())

    # 既知トークンと一緒に未知トークンがある場合は、モデル名の打ち間違いとみなす。
    # 黙って既定モデルで走らせると、指定したつもりの利用者が気づけない。
    if unknown:
        return TaskMode(
            profile_key=project_profile,
            prompt=text.strip(),
            unknown_model=unknown[0],
        )

    body = text[match.end():].strip()

    if wants_implement and not allow_implement:
        # キーワードは剥がさずに返す。拒否理由を伝えるだけで実行はしない。
        return TaskMode(profile_key=project_profile, prompt=text.strip(), denied=True)

    # 昇格と降格が同時に書かれたら、安全側（読み取り専用）を採る。
    if wants_investigate:
        profile_key = INVESTIGATE
    elif wants_implement:
        profile_key = IMPLEMENT
    else:
        profile_key = project_profile

    return TaskMode(
        profile_key=profile_key,
        prompt=body,
        model=models[0] if models else None,
    )


def implement_not_allowed_message(project_name: str) -> str:
    return (
        ":lock: このプロジェクトではコード変更が許可されていません。\n"
        f"Project: *{project_name}*\n\n"
        "`config/projects.yaml` の該当プロジェクトに `allow_implement: true` を設定し、"
        "Slack Bridge を再起動してください。"
    )


def unknown_model_message(token: str, allowed_models: list[str]) -> str:
    listed = " / ".join(f"`{m}`" for m in allowed_models)
    return (
        f":thinking_face: `{token}` というモデルは指定できません。\n\n"
        f"使えるモデル: {listed}\n\n"
        "例:\n"
        "```\n"
        "opus: 設計を詰めて\n"
        "実装 haiku: タイポを直して\n"
        "```"
    )
