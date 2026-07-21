"""Exec-env v2 — PowerShell + Monitor command-policy + the unknown-tool
catastrophe backstop + the Codex Windows-shell bridge routing.

Closes the Windows hole where the distinct ``PowerShell`` tool (and the
``Monitor`` background-command tool) bypassed ``_check_bash`` → ran fully ungated
in dontAsk/auto. Mirrors test_bash_policy_v2.py's structure: the dangerous-deny is
the safety floor (validated bare + wrapped + encoded), unknown→ask, destructive
flag, cross-user PATH gate on shared-admin.
"""

import base64
import sys

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from auth.path_policy import SecurityContext, check_tool_access


def _ctx(role="manager", username="alice", agent="personal-assistant",
         is_admin_agent=False) -> SecurityContext:
    return SecurityContext(role=role, username=username, agent=agent,
                           is_admin_agent=is_admin_agent)


def _ps(command: str, ctx: SecurityContext | None = None):
    decision, _ = check_tool_access("PowerShell", {"command": command}, ctx or _ctx())
    return decision


def _enc(inner: str) -> str:
    """Base64(UTF-16LE) — the -EncodedCommand wire form."""
    return base64.b64encode(inner.encode("utf-16-le")).decode()


# ===== Catastrophe guard — bare AND wrapped AND encoded =====

class TestPowerShellDangerous:
    BARE_DENY = [
        r"Remove-Item -Recurse -Force C:\\",
        "Remove-Item -Recurse -Force C:\\",          # single trailing backslash
        "Remove-Item -Force -Recurse C:/",
        "rm -r -fo /",
        "Remove-Item -Recurse -Force ~",
        "Remove-Item -Recurse -Force $HOME",
        "Remove-Item -Recurse -Force $env:SystemRoot",
        "Format-Volume -DriveLetter D",
        "Clear-Disk -Number 0",
        "Initialize-Disk 1",
        "Get-Content \\\\.\\PHYSICALDRIVE0",
        r"Remove-Item -Recurse -Force HKLM:\SOFTWARE\x",
    ]

    @pytest.mark.parametrize("cmd", BARE_DENY)
    def test_bare_dangerous_denied(self, cmd):
        assert not _ps(cmd).allowed, f"dangerous PS allowed: {cmd!r}"

    def test_codex_wrapped_dangerous_denied(self):
        # The wrapper string still contains the catastrophic substring → raw scan.
        assert not _ps("powershell.exe -Command 'Remove-Item -Recurse -Force C:\\'").allowed

    def test_cmd_syntax_catastrophes_denied(self):
        assert not _ps("cmd.exe /c rd /s /q C:\\").allowed
        assert not _ps("cmd /c format C:").allowed

    def test_encoded_dangerous_denied(self):
        assert not _ps(f"powershell -EncodedCommand {_enc('Remove-Item -Recurse -Force C:/')}").allowed
        assert not _ps(f"pwsh -e {_enc('Format-Volume -DriveLetter C')}").allowed

    def test_dangerous_denied_even_for_admin_role_nonadmin_agent(self):
        # Non-admin AGENT → no fast-path → dangerous applies even to an admin USER.
        assert not _ps("Remove-Item -Recurse -Force C:\\", _ctx(role="admin")).allowed

    @pytest.mark.parametrize("cmd", [
        # The classic false-deny risk: Format-* formatting cmdlets are BENIGN.
        "Get-Process | Format-Table -AutoSize",
        "Get-ChildItem | Format-List",
        "Get-Service | Format-Wide",
        # Non-root / scoped deletes are NOT catastrophic (→ destructive, prompts).
        # Own-scope / variable targets so neither the dangerous scan NOR the local
        # path check fires — isolating "the catastrophe pattern didn't match".
        "Remove-Item -Recurse -Force /users/alice/workspace/build",
        "Remove-Item -Recurse -Force /workspace/dist",
        "Remove-Item -Recurse -Force $tempDir",
    ])
    def test_benign_lookalikes_not_denied(self, cmd):
        assert _ps(cmd).allowed, f"false-denied benign PS: {cmd!r}"

    def test_nesting_depth_capped(self):
        # Pathological encoded nesting → denied at the depth cap, never unbounded.
        blob = "echo hi"
        for _ in range(12):
            blob = f"powershell -EncodedCommand {_enc(blob)}"
        # Each encoded level recurses once; 12 > _MAX_BASH_DEPTH (8) → deny.
        assert not _ps(blob).allowed


# ===== Tier classification (the UX win + correct prompt level) =====

class TestPowerShellTiers:
    @pytest.mark.parametrize("cmd,tier", [
        ("Get-Process", "read"),
        ("Get-ChildItem -Recurse", "read"),
        ("gci", "read"),
        ("Select-Object -First 5", "read"),
        ("Test-Path $foo", "read"),
        ("Clear-Host", "read"),
        ("Write-Output hi", "read"),
        ("Get-Process | Format-Table", "read"),
        ("Invoke-WebRequest https://x", "extended"),
        ("Invoke-Expression $x", "extended"),
        ("Start-Process notepad", "extended"),
        ("Foo-Bar -Baz 1", "ask"),          # unknown cmdlet → ask (never hard-deny)
        ("kubectl-ish-thing", "ask"),
    ])
    def test_tier(self, cmd, tier):
        d = _ps(cmd)
        assert d.allowed, f"{cmd!r} -> {d.reason}"
        assert d.permission_tier == tier, f"{cmd!r} -> {d.permission_tier!r} (want {tier!r})"

    def test_pipeline_max_tier(self):
        # read | extended → extended (max across segments). $x = variable (no path
        # extraction) so this isolates the tier logic from the cross-user check.
        d = _ps("Get-Content $x | Invoke-Expression")
        assert d.allowed and d.permission_tier == "extended"


# ===== Destructive flag (prompts even in acceptEdits) =====

class TestPowerShellDestructive:
    @pytest.mark.parametrize("cmd", [
        "Remove-Item $target",
        "Remove-Item $target -Recurse",
        "rm $x",
        "del $x",
        "Clear-Content $f",
    ])
    def test_destructive_flag(self, cmd):
        d = _ps(cmd)
        assert d.allowed and d.destructive is True, f"{cmd!r} not flagged destructive"

    def test_non_destructive_not_flagged(self):
        d = _ps("Set-Content $f -Value hi")
        assert d.allowed and not d.destructive

    def test_get_is_not_destructive(self):
        d = _ps("Get-ChildItem")
        assert d.allowed and not d.destructive


# ===== -EncodedCommand decode + recurse =====

class TestPowerShellEncodedCommand:
    def test_encoded_benign_classifies_inner(self):
        d = _ps(f"powershell -EncodedCommand {_enc('Get-Process')}")
        assert d.allowed  # inner is read; outer wrapper → at least "ask"

    def test_encoded_dangerous_denied(self):
        d = _ps(f"powershell -enc {_enc('Remove-Item -Recurse -Force C:/')}")
        assert not d.allowed

    def test_encoding_flag_not_treated_as_encoded(self):
        # -Encoding (Set/Get-Content param) must NOT trigger the -e decode path.
        d = _ps("Get-Content -Encoding utf8 $f")
        assert d.allowed and d.permission_tier == "read"


# ===== Codex shell-wrapper unwrap (proxy-side) =====

class TestPowerShellWrapperUnwrap:
    def test_command_wrapper_unwraps_to_inner_tier(self):
        # powershell.exe -Command 'Get-Process' → classified as the inner (read).
        d = _ps("powershell.exe -Command 'Get-Process'")
        assert d.allowed and d.permission_tier == "read"

    def test_cmd_c_unwraps(self):
        d = _ps("cmd.exe /c dir")
        assert d.allowed  # `dir` → alias get-childitem → read

    def test_pwsh_c_unwraps(self):
        d = _ps("pwsh -c 'Get-ChildItem'")
        assert d.allowed and d.permission_tier == "read"


# ===== Cross-user PATH gate (the load-bearing shared-admin boundary) =====
# Uses a LOCAL viewer ctx + sandbox-virtual /users/bob paths — the same mechanism
# the Bash cross-user tests use (_translate_sandbox_path → _check_read/write_path).

class TestPowerShellCrossUser:
    def test_cross_user_read_denied(self):
        d = _ps("Get-Content /users/bob/workspace/secret.txt", _ctx("viewer", "alice"))
        assert not d.allowed

    def test_own_user_read_allowed(self):
        d = _ps("Get-Content /users/alice/workspace/notes.txt", _ctx("viewer", "alice"))
        assert d.allowed

    def test_cross_user_write_denied(self):
        d = _ps("Set-Content /users/bob/workspace/x.txt -Value hi", _ctx("manager", "alice"))
        assert not d.allowed

    def test_cross_user_redirect_denied(self):
        d = _ps("Get-Process > /users/bob/workspace/out.txt", _ctx("manager", "alice"))
        assert not d.allowed

    def test_value_flag_value_not_path_checked(self):
        # -Value's argument must NOT be cross-user path-checked (regression: it was
        # extracted as a bogus write path → false-deny).
        d = _ps("Set-Content /users/alice/workspace/x.txt -Value hello", _ctx("manager", "alice"))
        assert d.allowed


# ===== Credential / agent-config backstops (raw) =====

class TestPowerShellBackstops:
    def test_agent_config_read_denied(self):
        # Scope-root .codex/auth.json reference → raw backstop.
        assert not _ps("Get-Content /workspace/.codex/auth.json", _ctx("admin", "alice")).allowed
        assert not _ps("Get-Content /users/alice/.claude/x.json", _ctx("admin", "alice")).allowed

    def test_agent_config_denied_in_codex_wrapper(self):
        assert not _ps(
            "powershell.exe -Command 'Get-Content /workspace/.codex/auth.json'",
            _ctx("admin", "alice"),
        ).allowed


# ===== Admin-tier role gating + admin-on-admin fast path =====

class TestPowerShellAdminGating:
    def test_host_control_denied_for_viewer(self):
        assert not _ps("Stop-Computer", _ctx("viewer", "alice")).allowed
        assert not _ps("Restart-Computer", _ctx("viewer", "alice")).allowed

    def test_host_control_allowed_for_admin(self):
        d = _ps("Restart-Computer", _ctx("admin", "alice"))
        assert d.allowed and d.permission_tier == "admin"

    def test_admin_on_admin_agent_unrestricted_for_normal_ops(self):
        # admin role + admin agent → unrestricted for non-catastrophe (host control,
        # destructive single-file, reads).
        admin = _ctx("admin", "alice", is_admin_agent=True)
        assert _ps("Stop-Computer", admin).allowed
        assert _ps("Get-Process", admin).allowed
        assert _ps("Remove-Item C:\\temp\\x.txt", admin).allowed  # destructive, not catastrophe

    def test_admin_on_admin_agent_catastrophe_still_denied(self):
        # Universal floor: even an admin agent cannot Format-Volume / wipe a drive
        # root — these are never a legitimate agent-issued action.
        admin = _ctx("admin", "alice", is_admin_agent=True)
        assert not _ps("Format-Volume -DriveLetter D", admin).allowed
        assert not _ps("Remove-Item -Recurse -Force C:\\", admin).allowed
        assert not _ps("Clear-Disk -Number 0", admin).allowed
        assert not _ps("cmd.exe /c rd /s /q C:\\", admin).allowed   # wrapped catastrophe too


# ===== Monitor routes through the Bash checker =====

class TestMonitorRoutesToBash:
    def _mon(self, command, ctx=None):
        decision, _ = check_tool_access("Monitor", {"command": command}, ctx or _ctx())
        return decision

    def test_monitor_dangerous_denied(self):
        assert not self._mon("rm -rf /").allowed

    def test_monitor_read_tier(self):
        d = self._mon("ps aux")
        assert d.allowed and d.permission_tier == "read"

    def test_monitor_extended_tier(self):
        d = self._mon("npm test")
        assert d.allowed and d.permission_tier == "extended"

    def test_monitor_unknown_asks(self):
        d = self._mon("kubectl get pods")
        assert d.allowed and d.permission_tier == "ask"


# ===== Unknown-tool catastrophe backstop (cross-platform, dangerous-only) =====

class TestUnknownToolBackstop:
    def _tool(self, name, ti, ctx=None):
        decision, _ = check_tool_access(name, ti, ctx or _ctx())
        return decision

    def test_unknown_tool_posix_dangerous_denied(self):
        assert not self._tool("SomeFutureShell", {"command": "rm -rf /"}).allowed

    def test_unknown_tool_powershell_dangerous_denied(self):
        assert not self._tool("SomeFutureShell", {"script": "Format-Volume -DriveLetter C"}).allowed

    def test_unknown_tool_benign_allowed(self):
        assert self._tool("SomeFutureShell", {"command": "echo hello"}).allowed

    def test_known_structured_tool_not_content_filtered(self):
        # A benign NL arg mentioning rm -rf MUST NOT be denied (BLOCKER fix: the
        # backstop is dangerous-only AND scoped to unknown tools — TodoWrite is
        # known-structured, so its NL args are never scanned).
        d = self._tool("TodoWrite", {"todos": "remember to rm -rf / the stale build dir"})
        assert d.allowed

    def test_agent_tool_with_path_arg_allowed(self):
        d = self._tool("Agent", {"prompt": "read /users/bob/notes and the .codex/auth.json config"})
        assert d.allowed


# ===== Codex bridge: powershell.exe wrapper → ("PowerShell", …) routing =====

class TestCodexBridgePowerShellRouting:
    def _route(self, command_list_or_actions, *, v2=False):
        from core.layers.codex import codex_approvals as ca
        if v2:
            params = {"commandActions": [{"command": command_list_or_actions}]}
            return ca.approval_to_tool("item/commandExecution/requestApproval", params)
        params = {"command": command_list_or_actions}
        return ca.approval_to_tool("execCommandApproval", params)

    def test_legacy_bash_routes_to_bash(self):
        tn, ti = self._route(["bash", "-lc", "curl x | head"])
        assert tn == "Bash" and "curl" in ti["command"]

    def test_legacy_powershell_routes_to_powershell(self):
        tn, ti = self._route(["powershell.exe", "-Command", "Get-Process"])
        assert tn == "PowerShell" and "Get-Process" in ti["command"]

    def test_v2_cmd_c_routes_to_powershell(self):
        tn, ti = self._route("cmd.exe /c dir", v2=True)
        assert tn == "PowerShell"

    def test_v2_pwsh_routes_to_powershell(self):
        tn, ti = self._route("pwsh -c Get-ChildItem", v2=True)
        assert tn == "PowerShell"

    def test_v2_bash_stays_bash(self):
        tn, ti = self._route("ls -la /tmp", v2=True)
        assert tn == "Bash"

    def test_build_response_keys_off_method_not_tool(self):
        # Routing to "PowerShell" must NOT break the JSON-RPC response shaping
        # (build_response keys off `method`, not tool_name).
        from core.layers.codex import codex_approvals as ca
        assert ca.build_response("item/commandExecution/requestApproval", {}, True) == {"decision": "accept"}
        assert ca.build_response("item/commandExecution/requestApproval", {}, False) == {"decision": "decline"}
