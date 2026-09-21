import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.formatting import denials_note, split_for_slack, to_mrkdwn  # noqa: E402


def test_heading_becomes_bold():
    assert to_mrkdwn("## 推奨構成") == "*推奨構成*"
    assert to_mrkdwn("# A\n### B") == "*A*\n*B*"


def test_bold_is_converted_to_single_asterisk():
    assert to_mrkdwn("これは **重要** です") == "これは *重要* です"
    assert to_mrkdwn("__strong__") == "*strong*"


def test_bullets_become_dots():
    assert to_mrkdwn("- one\n* two\n  - nested") == "• one\n• two\n  • nested"


def test_links_become_slack_format():
    assert to_mrkdwn("[docs](https://example.com/a)") == "<https://example.com/a|docs>"


def test_code_block_contents_are_untouched():
    src = "```python\n## not a heading\n**not bold**\n- not a bullet\n```"
    assert to_mrkdwn(src) == src


def test_unclosed_code_fence_is_closed():
    assert to_mrkdwn("```\nx").endswith("```")


def test_short_text_is_single_chunk():
    assert split_for_slack("hello") == ["hello"]


def test_empty_text_yields_no_chunks():
    assert split_for_slack("   ") == []


def test_long_text_is_split_within_limit():
    text = "\n".join(f"line {i}" for i in range(2000))
    chunks = split_for_slack(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2900 for c in chunks)
    rejoined = "\n".join(chunks)
    assert "line 0" in rejoined and "line 1999" in rejoined


def test_split_keeps_code_fences_balanced():
    body = "\n".join(f"    code line {i}" for i in range(500))
    chunks = split_for_slack(f"```\n{body}\n```")
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:80]


def test_very_long_single_line_is_hard_wrapped():
    chunks = split_for_slack("x" * 10000)
    assert all(len(c) <= 2900 for c in chunks)
    assert sum(c.count("x") for c in chunks) == 10000


def test_denials_note_is_empty_when_nothing_denied():
    assert denials_note([]) == ""


def test_denials_note_lists_unique_tools():
    note = denials_note([{"tool_name": "Bash"}, {"tool_name": "Bash"}, {"tool_name": "Write"}])
    assert "`Bash`" in note and "`Write`" in note
    assert note.count("Bash") == 1
