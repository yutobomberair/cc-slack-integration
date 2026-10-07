"""``model:`` と ``effort:`` — スレッドのモデルと推論の強さを切り替える。

インライン指定（``max opus: 直して``）はその依頼だけに効く。こちらは ``cd:`` と同じく
**スレッド単位で持続**するので、前置きを毎回打たずに済む。

    @ClaudeCode model: sonnet     このスレッドは Sonnet で
    @ClaudeCode effort: max       推論を最大で
    @ClaudeCode model             いまの設定を見る
    @ClaudeCode model: 既定       プロジェクトの既定に戻す

``共有:`` や ``cd:`` と同じくブリッジが直接処理するので Claude Code は起動しない。

effort を上げるとトークンを多く使う。つまり**利用枠の消費に直接響く**ので、
``usage`` で枠の残りを見てから決められるよう、設定時に残量も添える。
"""

from __future__ import annotations

_MODEL_TOKENS = ("model", "/model", "モデル")
_EFFORT_TOKENS = ("effort", "/effort", "推論", "強度")

#: 既定へ戻す指定。``cd: .`` と同じ考え方。
RESET_TOKENS = {"", "既定", "default", "reset", "auto", "-", "なし", "解除"}


def parse_directive(prompt: str) -> tuple[str, str] | None:
    """``model: sonnet`` なら ``("model", "sonnet")``。

    ``model`` 単独なら ``("model", "")``（いまの設定を見る）。該当しなければ None。
    ``ls`` と同じくコロンを省ける。先頭行だけを見るので、本文中の「model」には
    反応しない。
    """
    text = (prompt or "").strip()
    if not text:
        return None
    head = text.splitlines()[0].strip()

    for field, tokens in (("model", _MODEL_TOKENS), ("effort", _EFFORT_TOKENS)):
        if head.lower() in tokens:
            return field, ""
        for separator in (":", "："):
            key, sep, value = head.partition(separator)
            if sep and key.strip().lower() in tokens:
                return field, value.strip()
        parts = head.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in tokens:
            return field, parts[1].strip()
    return None


def validate(field: str, value: str, allowed: list[str]) -> str | None:
    """指定された値を正規化する。既定へ戻す指定なら空文字、駄目なら None。"""
    text = (value or "").strip().strip("`\"'").lower()
    if text in RESET_TOKENS:
        return ""
    return text if text in {a.lower() for a in allowed} else None


def _label(field: str) -> str:
    return "モデル" if field == "model" else "推論の強さ"


def changed_message(field: str, value: str, effective: str, usage_note: str = "") -> str:
    """設定したときの返信。"""
    if value:
        head = f":brain: このスレッドの{_label(field)}を `{value}` にしました。"
    else:
        head = (
            f":brain: このスレッドの{_label(field)}の指定を解除しました"
            f"（既定の `{effective or '(Claude Code の既定)'}` に戻ります）。"
        )
    text = head + (
        "\n\n以降このスレッドの依頼に適用します"
        f"（`{field}: 既定` で戻せます）。"
    )
    if field == "effort" and value:
        text += (
            "\n\n:warning: 推論を上げるとトークンを多く使うため、利用枠の消費が速く"
            "なります。"
        )
    if usage_note:
        text += f"\n{usage_note}"
    return text


def current_message(
    thread_values: dict[str, str],
    project_model: str | None,
    project_effort: str | None,
    allowed_models: list[str],
    allowed_efforts: list[str],
) -> str:
    """いまの設定と、どこから来た値かを示す。

    「指定したつもりが効いていない」を見つけられるよう、スレッド設定と既定を
    並べて出す。
    """
    lines = [":brain: *このスレッドの設定*", ""]
    for field, default in (("model", project_model), ("effort", project_effort)):
        chosen = thread_values.get(field, "")
        if chosen:
            lines.append(f"{_label(field)}: `{chosen}`（このスレッドの指定）")
        elif default:
            lines.append(f"{_label(field)}: `{default}`（プロジェクトの既定）")
        else:
            lines.append(f"{_label(field)}: 指定なし（Claude Code の既定）")
    lines.append("")
    lines.append("使えるモデル: " + " / ".join(f"`{m}`" for m in allowed_models))
    lines.append("使える推論の強さ: " + " / ".join(f"`{e}`" for e in allowed_efforts))
    lines.append("")
    lines.append(
        "変更: `model: sonnet` / `effort: max`。その依頼だけなら `max opus: 直して`。"
    )
    lines.append("実際に使われたモデルは完了メッセージに出ます。")
    return "\n".join(lines)


def error_message(field: str, token: str, allowed: list[str]) -> str:
    listed = " / ".join(f"`{a}`" for a in allowed)
    return (
        f":thinking_face: `{token}` は{_label(field)}として指定できません。\n\n"
        f"使えるのは: {listed}\n"
        f"既定に戻すには `{field}: 既定`。"
    )
