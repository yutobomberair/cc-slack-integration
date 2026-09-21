import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.claude_runner import build_command  # noqa: E402
from app.config import PermissionProfile, Runtime  # noqa: E402


def _cmd(profile, runtime=None, **kw):
    return build_command("claude.exe", profile, runtime or Runtime(), **kw)


def test_investigate_profile_is_read_only():
    profile = PermissionProfile(key="investigate", tools=["Read", "Grep", "Glob"])
    cmd = _cmd(profile)
    tools = cmd[cmd.index("--tools") + 1]
    assert "Bash" not in tools
    assert "Edit" not in tools
    assert "Write" not in tools


def test_permission_prompts_none_is_always_present():
    cmd = _cmd(PermissionProfile(key="investigate"))
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"


def test_dangerous_skip_flag_is_never_emitted():
    profile = PermissionProfile(
        key="implement", permission_mode="acceptEdits", max_budget_usd=8.0
    )
    joined = " ".join(_cmd(profile))
    assert "dangerously-skip-permissions" not in joined


def test_json_output_format_is_requested():
    cmd = _cmd(PermissionProfile(key="investigate"))
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_prompt_is_not_passed_as_argv():
    # プロンプトは stdin 経由。argv に混ざらないことを保証する。
    cmd = _cmd(PermissionProfile(key="investigate"))
    assert cmd[1] == "-p"
    assert not any(a.startswith("Reply") or "依頼" in a for a in cmd)


def test_mcp_servers_are_disabled_by_default():
    # アカウント単位の MCP サーバを読み込むと、無人実行では拒否されるだけ無駄になる
    assert "--strict-mcp-config" in _cmd(PermissionProfile(key="investigate"))


def test_mcp_can_be_re_enabled_by_config():
    cmd = _cmd(PermissionProfile(key="investigate"), Runtime(strict_mcp_config=False))
    assert "--strict-mcp-config" not in cmd


def test_implement_profile_blocks_push_and_merge():
    profile = PermissionProfile(
        key="implement",
        permission_mode="acceptEdits",
        disallowed_tools=["Bash(git push*)", "Bash(git merge*)"],
    )
    joined = " ".join(_cmd(profile))
    assert "Bash(git push*)" in joined
    assert "Bash(git merge*)" in joined
    assert "--permission-mode acceptEdits" in joined


def test_model_is_passed_when_configured():
    cmd = _cmd(PermissionProfile(key="investigate"), Runtime(model="opus"))
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_session_id_defaults_to_creating_a_new_session():
    # resume を指定しなければ新規作成。継続は test_session.py で検証している。
    cmd = _cmd(PermissionProfile(key="investigate"), session_id="abc-123")
    assert cmd[cmd.index("--session-id") + 1] == "abc-123"
    assert "--resume" not in cmd


def test_no_session_flags_when_session_id_is_absent():
    cmd = _cmd(PermissionProfile(key="investigate"))
    assert "--session-id" not in cmd
    assert "--resume" not in cmd
