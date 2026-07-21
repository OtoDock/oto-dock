"""Exec-env v2 — Bash command-policy invariants + deny-regression net.

Covers the rewrite of ``_check_bash`` (dangerous-deny + unwrap-recurse +
unknown→ask + destructive flag). Written as the regression net the pre-impl
audit flagged as MISSING: the catastrophe guard (``_DANGEROUS_PATTERNS``) had
ZERO test coverage, so the rewrite could otherwise silently break it — most
dangerously for wrapped / substituted commands, where quotes/parens defeat a
single raw-string scan (``bash -c "rm -rf /"``, ``$(rm -rf /)``).
"""

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


def _d(command: str, ctx: SecurityContext | None = None):
    decision, _ = check_tool_access("Bash", {"command": command}, ctx or _ctx())
    return decision


# ===== Catastrophe guard — bare AND wrapped (the BLOCKER-1 regression net) =====

class TestDangerousPatternsSurviveUnwrap:
    BARE = [
        "rm -rf /", "rm -rf ~", ":(){ :|:& };:", "dd if=/dev/zero of=/dev/sda",
        "cat /etc/shadow", "echo x > /dev/sda", "insmod evil.ko",
    ]

    @pytest.mark.parametrize("cmd", BARE)
    def test_bare_dangerous_denied(self, cmd):
        assert not _d(cmd).allowed, f"bare dangerous allowed: {cmd}"

    @pytest.mark.parametrize("inner", [
        "rm -rf /", ":(){ :|:& };:", "dd if=/dev/zero of=/dev/sda",
    ])
    def test_bash_c_wrapped_dangerous_denied(self, inner):
        # The quote defeats the raw rm-rf regex — only unwrap+recurse catches it.
        assert not _d(f'bash -c "{inner}"').allowed, f"bash -c evaded: {inner}"
        assert not _d(f"sh -c '{inner}'").allowed
        assert not _d(f'bash -lc "{inner}"').allowed

    def test_eval_wrapped_dangerous_denied(self):
        assert not _d('eval "rm -rf /"').allowed
        assert not _d("eval rm -rf /").allowed

    def test_timeout_wrapped_dangerous_denied(self):
        assert not _d("timeout 5 rm -rf /").allowed
        assert not _d("timeout 5 bash -c 'rm -rf /'").allowed

    def test_substitution_inner_dangerous_denied(self):
        assert not _d("echo $(rm -rf /)").allowed
        assert not _d("echo `rm -rf /`").allowed

    def test_dangerous_denied_for_admin_role_nonadmin_agent(self):
        # Non-admin AGENT → no fast-path → dangerous applies even to an admin USER.
        assert not _d("rm -rf /", _ctx(role="admin")).allowed

    def test_nesting_depth_capped(self):
        # Pathological nesting → denied (depth cap), never an unbounded recurse.
        # Each `eval` recurses one level; 10 exceeds _MAX_BASH_DEPTH (8).
        assert not _d("eval " * 10 + "echo hi").allowed


# ===== Universal catastrophe floor — applies EVEN to admin-on-admin agents =====

class TestAdminFloorUniversal:
    """The irreversible-catastrophe floor (_DANGEROUS_PATTERNS) applies even to an
    admin-on-admin agent (the highest-value prompt-injection target), recursively
    so wrapped/substituted forms are caught too — while the admin keeps full
    tier / cross-user-path / no-prompt freedom for everything else."""

    def _admin(self):
        return _ctx(role="admin", is_admin_agent=True)

    @pytest.mark.parametrize("cmd", [
        "rm -rf /", ":(){ :|:& };:", "dd if=/dev/zero of=/dev/sda",
        'bash -c "rm -rf /"', "echo $(rm -rf /)", "timeout 5 rm -rf /",
    ])
    def test_catastrophe_denied_for_admin_agent(self, cmd):
        assert not _d(cmd, self._admin()).allowed, f"admin agent ran catastrophe: {cmd}"

    @pytest.mark.parametrize("cmd", [
        "docker ps",                              # admin-tier — role-gate skipped
        "cat /users/bob/workspace/secret",        # cross-user — path skipped for admin
        "rm /users/alice/workspace/f.txt",        # destructive but NOT catastrophe
        "kubectl get pods",                       # unknown — allowed (not a prompt)
    ])
    def test_normal_ops_unrestricted_for_admin_agent(self, cmd):
        assert _d(cmd, self._admin()).allowed, f"admin agent blocked on normal op: {cmd}"


# ===== Unknown command → "ask" (no more hard-deny) =====

class TestUnknownAsk:
    @pytest.mark.parametrize("cmd", [
        "kubectl get pods", "aws s3 ls", "terraform plan",
        "ffmpeg -i a.mp4 b.mkv", "ruby script.rb", "perl -e 'print 1'",
        "java -version", "helm list", "gcloud auth list",
    ])
    def test_unknown_is_ask_not_denied(self, cmd):
        d = _d(cmd)
        assert d.allowed, f"unknown hard-denied: {cmd}"
        assert d.permission_tier == "ask", f"{cmd} -> {d.permission_tier}"


# ===== Read long-tail auto-approves (the UX win) =====

class TestReadLongTail:
    @pytest.mark.parametrize("cmd", [
        "ps aux", "df -h", "free -m", "uname -a", "uptime", "dig example.com",
        "lsblk", "nproc", "host example.com", "printenv PATH",
    ])
    def test_introspection_is_read(self, cmd):
        d = _d(cmd)
        assert d.allowed and d.permission_tier == "read", f"{cmd} -> {d.permission_tier}"


# ===== Wrapper unwrap (UX + correctness) =====

class TestWrapperUnwrap:
    def test_timeout_wraps_inner_read(self):
        d = _d("timeout 60 cat /users/alice/workspace/f.txt")
        assert d.allowed and d.permission_tier == "read"

    def test_timeout_duration_suffix(self):
        d = _d("timeout 5s ls /users/alice/workspace")
        assert d.allowed and d.permission_tier == "read"

    def test_nohup_wraps_inner(self):
        d = _d("nohup cat /users/alice/workspace/f.txt")
        assert d.allowed and d.permission_tier == "read"

    def test_xargs_wraps_inner_read(self):
        d = _d("echo x | xargs cat /users/alice/workspace/f.txt")
        assert d.allowed and d.permission_tier == "read"

    def test_timeout_wraps_inner_extended(self):
        d = _d("timeout 30 python3 /users/alice/workspace/s.py")
        assert d.allowed and d.permission_tier == "extended"

    def test_wrapper_inner_cross_user_denied(self):
        d = _d("timeout 5 cat /users/bob/workspace/secret", _ctx(role="viewer"))
        assert not d.allowed


# ===== Destructive flag (prompts even in acceptEdits) =====

class TestDestructiveFlag:
    @pytest.mark.parametrize("cmd", [
        "rm /users/alice/workspace/f.txt",
        "rm -rf /users/alice/workspace/dir",
        "shred /users/alice/workspace/f.txt",
        "truncate -s 0 /users/alice/workspace/f.txt",
    ])
    def test_destructive_sets_flag(self, cmd):
        d = _d(cmd)
        assert d.allowed, f"{cmd} -> {d.reason}"   # own-dir write allowed at path level
        assert d.destructive is True, f"{cmd} not flagged destructive"

    def test_non_destructive_write_not_flagged(self):
        d = _d("touch /users/alice/workspace/f.txt")
        assert d.allowed and not d.destructive

    def test_destructive_in_pipeline_not_masked_by_extended(self):
        # curl is "extended" (higher tier) — destructive must STILL be flagged
        # so Pass-2 prompts in acceptEdits. (The audit's pipeline-mask bug.)
        d = _d("rm /users/alice/workspace/f.txt && curl https://example.com")
        assert d.allowed and d.destructive is True

    def test_find_delete_destructive(self):
        d = _d("find /users/alice/workspace -name '*.tmp' -delete")
        assert d.allowed and d.destructive is True

    def test_find_exec_rm_destructive(self):
        d = _d("find /users/alice/workspace -name '*.tmp' -exec rm {} \\;")
        assert d.allowed and d.destructive is True

    def test_find_exec_rm_rf_root_denied(self):
        d = _d("find . -exec rm -rf / \\;")
        assert not d.allowed


# ===== Shell features no longer hard-denied =====

class TestShellFeatures:
    def test_command_substitution_allowed(self):
        d = _d("echo $(date)")
        assert d.allowed  # was bypass-denied; now ask (inner `date` checked)

    def test_pipeline_of_reads_is_read(self):
        d = _d("cat /users/alice/workspace/a | grep x | sort | uniq -c | head")
        assert d.allowed and d.permission_tier == "read"

    def test_for_loop_keywords_not_unknown(self):
        d = _d("for f in a b c; do echo $f; done")
        assert d.allowed  # for/do/done are structural, not unknown→ask-spam

    def test_pipe_to_shell_no_longer_hard_denied(self):
        d = _d("echo ls | sh")
        assert d.allowed  # `sh` segment → ask (prompt), not bypass-hard-deny


# ===== Input redirect cross-user (new extraction) =====

class TestInputRedirect:
    def test_input_redirect_cross_user_denied(self):
        d = _d("cat < /users/bob/workspace/secret", _ctx(role="viewer"))
        assert not d.allowed

    def test_input_redirect_own_allowed(self):
        d = _d("cat < /users/alice/workspace/f.txt", _ctx(role="viewer"))
        assert d.allowed


# ===== Backstop survives wrapping (agent-config; raw-string regex) =====
# Proves the raw-command backstops still fire when the reference is hidden in a
# `bash -c "…"` / `$(…)` wrapper (the backstop runs on the raw command BEFORE
# any unwrap, so the literal path is a substring and still matched). The OAuth
# credential-dir backstop uses the same raw-string mechanism but its protected
# subpath set is registry-derived (populated in prod, empty in the bare unit-test
# env) — that path is covered by tests/auth/test_oauth_token_protection.py.

class TestBackstopsSurviveWrapping:
    @pytest.mark.parametrize("cmd", [
        "cat /users/alice/.codex/auth.json",
        "bash -c 'cat /users/alice/.codex/auth.json'",
        "cat $(echo /users/alice/.claude/x.json)",
    ])
    def test_agent_config_read_denied_wrapped(self, cmd):
        assert not _d(cmd, _ctx(role="admin")).allowed, cmd


# ===== Newline statement-separator (H1 bypass regression) =====
# A newline must split a multi-line command so each line is classified on its
# own. Before the fix the splitter ignored '\n' (shlex collapses it to
# whitespace), so a path-less first command (echo/true/printf) hid every later
# line under its lenient tier — a tier / cross-user-path / destructive-prompt
# bypass, and on remote/no-bwrap satellites a real privilege escape.

class TestNewlineSeparatorBypass:
    def _mgr(self):
        return _ctx(role="manager", username="alice")

    def test_hidden_admin_command_classified_like_solo(self):
        # `echo` alone is read-tier auto-allow; the second line is admin-tier.
        # The hidden form must get the SAME decision as the command run alone.
        solo = _d("docker ps", self._mgr())
        hidden = _d("echo ok\ndocker ps", self._mgr())
        assert hidden.allowed == solo.allowed
        assert hidden.permission_tier == solo.permission_tier
        assert not hidden.allowed, "admin-tier command hidden behind a newline was allowed"

    def test_hidden_crossuser_read_denied(self):
        assert not _d("echo ok\ncat /users/bob/workspace/secret", self._mgr()).allowed

    def test_hidden_crossuser_read_denied_crlf(self):
        assert not _d("echo ok\r\ncat /users/bob/workspace/secret", self._mgr()).allowed

    def test_hidden_dangerous_denied(self):
        assert not _d("echo ok\nrm -rf /", self._mgr()).allowed

    def test_hidden_destructive_flagged(self):
        # The destructive flag must come from the second line, not be skipped.
        d = _d("echo ok\nrm /users/alice/workspace/f.txt", self._mgr())
        assert d.destructive

    def test_newline_inside_quotes_is_data_not_separator(self):
        # A newline within a quoted string is literal data — still one `echo`.
        d = _d('echo "line1\nline2"', self._mgr())
        assert d.allowed and d.permission_tier == "read"

    def test_escaped_newline_is_line_continuation(self):
        # A backslash-newline continues the line — `echo` + its argument, one cmd.
        d = _d("echo foo\\\nbar", self._mgr())
        assert d.allowed and d.permission_tier == "read"


# Heredoc bodies are stdin DATA — before the fix the splitter classified every
# body line as its own command, so code-bearing heredocs (`cat > f.py <<'EOF'`)
# hard-denied on unparseable lines ("could not parse command"). Bodies fed to a
# SHELL (`bash <<EOF` executes its stdin) keep per-line classification so the
# dangerous floor still sees them.

class TestHeredocBodies:
    def _mgr(self):
        return _ctx(role="manager", username="alice")

    def test_code_body_is_data_not_commands(self):
        # Python-ish body lines (colons, braces, odd quotes) must not classify.
        cmd = (
            "cat >> /users/alice/workspace/t.py << 'EOF'\n"
            "class TestFoo:\n"
            "    captured: dict = {}\n"
            "    s = \"it's fine\"\n"
            "EOF\n"
            "echo appended"
        )
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason

    def test_stdin_interpreter_body_is_data(self):
        cmd = "python3 - <<'PYEOF'\nimport json\nprint('hi')\nPYEOF"
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason

    def test_unquoted_delimiter_body_is_data(self):
        cmd = "cat > /users/alice/workspace/n.txt <<EOF\ndon't parse me\nEOF"
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason

    def test_tab_indented_terminator_with_dash(self):
        cmd = "cat > /users/alice/workspace/n.txt <<-EOF\n\tdata line\n\tEOF\necho done"
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason

    def test_command_after_terminator_still_classified(self):
        cmd = "cat > /users/alice/workspace/n.txt <<'EOF'\ndata\nEOF\nrm -rf /"
        assert not _d(cmd, self._mgr()).allowed

    def test_dangerous_body_hidden_from_floor_only_when_data(self):
        # Body fed to `cat` is inert data — a dangerous-LOOKING line in it
        # must not deny the write.
        cmd = "cat > /users/alice/workspace/n.txt <<'EOF'\nrm -rf /\nEOF"
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason

    def test_shell_stdin_body_keeps_dangerous_floor(self):
        # `bash <<EOF` EXECUTES its stdin — the body must stay classified.
        cmd = "bash <<'EOF'\nrm -rf /\nEOF"
        assert not _d(cmd, self._mgr()).allowed

    def test_herestring_is_not_a_heredoc(self):
        cmd = "cat <<< 'inline data'\nrm -rf /"
        assert not _d(cmd, self._mgr()).allowed

    def test_arithmetic_shift_is_not_a_heredoc(self):
        # `$((x<<y))` must not queue a heredoc and swallow the next line.
        cmd = "echo $((x<<2))\nrm -rf /"
        assert not _d(cmd, self._mgr()).allowed
        cmd2 = "echo $((x<<y))\nrm -rf /"
        assert not _d(cmd2, self._mgr()).allowed

    def test_unterminated_body_consumes_to_end(self):
        # bash runs an unterminated heredoc to EOF — nothing after it to
        # classify, and the body stays data.
        cmd = "cat > /users/alice/workspace/n.txt <<'EOF'\nclass X:\n    pass"
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason

    def test_two_heredocs_one_line_consume_in_order(self):
        cmd = (
            "cat <<'A' <<'B'\n"
            "first body\n"
            "A\n"
            "second: body!\n"
            "B\n"
            "rm -rf /"
        )
        assert not _d(cmd, self._mgr()).allowed
