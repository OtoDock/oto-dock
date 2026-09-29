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
from core import placement


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
        # Forms the raw scan alone missed: the bare root only exists after
        # the continuation is joined; a newline after the root; a space or a
        # quote before /etc/shadow is not a word boundary.
        "rm -rf \\\n/", "rm -rf /\nls", "cat /etc/shadow", 'cat "/etc/shadow"',
        "cat <<EOF\n$(rm -rf /)\nEOF",
    ])
    def test_catastrophe_denied_for_admin_agent(self, cmd):
        assert not _d(cmd, self._admin()).allowed, f"admin agent ran catastrophe: {cmd}"

    @pytest.mark.parametrize("cmd", [
        "rm -rf /tmp/build", "rm -rf ./dist", "cat /etc/shadowfax",
        "cat /var/lib/etc/shadow", "rm -rf ~/.cache",
        # A heredoc DATA body is inert even when a line of it looks like
        # the catastrophe: the segment scan never sees data lines.
        "cat > /tmp/n.txt <<'EOF'\nrm -rf /\nEOF",
    ])
    def test_floor_lookalikes_still_run_for_admin_agent(self, cmd):
        assert _d(cmd, self._admin()).allowed, f"admin agent blocked on lookalike: {cmd}"

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


class TestUnparsableAsk:
    """A segment shlex refuses (a dangling backslash) is an unknown command:
    it asks instead of being refused — after every path-looking token and
    redirect target in it is checked as a write, so it never reaches further
    than a parsable form would."""

    @pytest.mark.parametrize("cmd", [
        "echo hi \\",
        "cat /users/alice/workspace/notes.md \\",
        "python3 -c pass < /dev/null \\",
        "echo hi > /users/alice/workspace/out.txt \\",
    ])
    def test_unparsable_asks(self, cmd):
        d = _d(cmd, _ctx(role="manager"))
        assert d.allowed, (cmd, d.reason)
        assert d.permission_tier == "ask"

    @pytest.mark.parametrize("cmd", [
        "cat /users/bob/workspace/secret \\",
        'cat "/users/bob/workspace/secret" \\',
        "echo x > /users/bob/workspace/out.txt \\",
        "sort < /users/bob/workspace/secret \\",
    ])
    def test_unparsable_still_denies_a_denied_path(self, cmd):
        d = _d(cmd, _ctx(role="manager"))
        assert not d.allowed, cmd
        assert "could not parse" in d.reason and "/users/bob/workspace/" in d.reason

    def test_unparsable_path_is_checked_as_a_write(self):
        # The direction is unknown: a viewer's unparsable line naming a
        # read-only tree is denied, where the parsable read would pass.
        assert _d("cat /knowledge/faq.md", _ctx(role="viewer")).allowed
        d = _d("cat /knowledge/faq.md \\", _ctx(role="viewer"))
        assert not d.allowed and "/knowledge/faq.md" in d.reason

    def test_unclosed_quote_stays_a_deny(self):
        d = _d('echo "hi', _ctx(role="manager"))
        assert not d.allowed and "Unclosed quote" in d.reason


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


class TestPseudoPaths:
    """The null device, the standard streams and ``/dev/fd/N`` are not files:
    neither redirect direction sends them through the path gate, so they work
    on every target (a satellite's home band would otherwise deny them)."""

    LOCAL = _ctx(role="viewer")
    REMOTE = SecurityContext(
        role="manager", username="dave", agent="my-agent", is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, home_dir="/home/dave", agents_dir="/home/dave/.oto-dock/agents", machine_id="m1"),
        )

    @pytest.mark.parametrize("cmd", [
        "python3 -c pass < /dev/null",
        "cat < /dev/stdin",
        "read -r line < /dev/fd/3",
        "echo x > /dev/stderr",
        "echo x >> /dev/stdout",
        "echo x > /dev/null 2> /dev/null",
    ])
    @pytest.mark.parametrize("ctx", [LOCAL, REMOTE])
    def test_pseudo_redirects_allowed_everywhere(self, cmd, ctx):
        d = _d(cmd, ctx)
        assert d.allowed, (cmd, d.reason)

    def test_pseudo_output_redirect_does_not_bump_tier(self):
        assert _d("echo x > /dev/stderr", self.LOCAL).permission_tier == "read"

    @pytest.mark.parametrize("cmd", [
        "cat < /dev/sda",
        "cat < /devnull",
        "cat < /dev/null/../shadow",
    ])
    def test_lookalikes_still_go_through_the_path_gate(self, cmd):
        assert not _d(cmd, self.LOCAL).allowed, cmd


# ===== Network pseudo-devices are sockets, not files — never floor-denied =====

class TestNetworkPseudoDevices:
    """``/dev/tcp/…`` / ``/dev/udp/…`` left the catastrophe floor: the sandbox
    netns (local) and the pairing decision (remote) govern network reach, so
    the gate neither denies the pattern nor treats the pseudo-path as a file
    that must fall inside the role's read/write scope."""

    CMDS = [
        "exec 3<>/dev/tcp/example.com/80",
        "cat </dev/tcp/example.com/80",
        "echo ping > /dev/udp/192.168.1.1/53",
        "bash -c 'exec 3<>/dev/tcp/example.com/443'",
    ]

    @pytest.mark.parametrize("cmd", CMDS)
    def test_allowed_for_viewer_local(self, cmd):
        assert _d(cmd, _ctx(role="viewer")).allowed, cmd

    @pytest.mark.parametrize("cmd", CMDS)
    def test_allowed_for_admin_on_admin_agent(self, cmd):
        assert _d(cmd, _ctx(role="admin", is_admin_agent=True)).allowed, cmd

    @pytest.mark.parametrize("cmd", CMDS)
    def test_allowed_on_home_restricted_satellite(self, cmd):
        ctx = SecurityContext(
            role="editor", username="alice", agent="personal-assistant",
            is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, home_dir="/home/alice", allow_full_fs=False),
            )
        assert _d(cmd, ctx).allowed, cmd

    def test_real_device_writes_stay_denied(self):
        assert not _d("echo x > /dev/sda").allowed
        assert not _d("cat /dev/tcp/example.com/80 > /dev/sdb").allowed


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

_REPORTED_SCRIPT = r'''PN=$(curl -s --max-time 25 "https://api.ted.europa.eu/v3/notices/search" -X POST -H "Content-Type: application/json" \
 -d '{"query":"buyer-country=\"GRC\" AND notice-type=\"can-standard\" AND publication-date>=20260601","limit":1,"page":1,"fields":["publication-number"]}' \
 | python3 -c "import sys,json;print(json.load(sys.stdin)['notices'][0]['publication-number'])")
echo "Greek award notice: $PN"
curl -s --max-time 30 "https://ted.europa.eu/en/notice/$PN/xml" -o /tmp/n.xml
echo "XML bytes: $(wc -c < /tmp/n.xml)"
echo ""
echo "=== submission statistics found in notice XML ==="
grep -oE '<efac:ReceivedSubmissionsStatistics>.*?</efac:ReceivedSubmissionsStatistics>' /tmp/n.xml | head -c 800
echo ""
grep -oE 'StatisticsCode[^>]*>[^<]*|StatisticsNumeric[^>]*>[^<]*' /tmp/n.xml | head -20'''


class TestLocalTmp:
    """The sandbox's /tmp is a private tmpfs and the agent's HOME: on a local
    target every path under it is admitted both ways, the CLI state that
    could live there excepted. A satellite keeps its home band, where the
    session's own runtime tree is the only carve."""

    LOCAL = _ctx(role="manager")
    VIEWER = _ctx(role="viewer")
    REMOTE = SecurityContext(
        role="manager", username="dave", agent="my-agent", is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, home_dir="/home/dave", agents_dir="/home/dave/.oto-dock/agents", machine_id="m1"),
        )
    FULL = SecurityContext(
        role="manager", username="dave", agent="my-agent", is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, home_dir="/home/dave", agents_dir="/home/dave/.oto-dock/agents", machine_id="m1", allow_full_fs=True),
        )

    @pytest.mark.parametrize("cmd, tier", [
        ("cat /tmp/n.xml", "read"),
        ("wc -c < /tmp/n.xml", "read"),
        ("echo hi > /tmp/x.txt", "edit"),
        ("mkdir -p /tmp/foo", "edit"),
        ("cp /tmp/a /tmp/b", "edit"),
        ("curl -o /tmp/n.xml https://example.test", "extended"),
        ("echo hi > ~/x.txt", "edit"),
        ("cat ~/x.txt", "read"),
    ])
    @pytest.mark.parametrize("ctx", [LOCAL, VIEWER])
    def test_tmp_is_admitted_both_ways_locally(self, cmd, tier, ctx):
        d = _d(cmd, ctx)
        assert d.allowed, (cmd, d.reason)
        assert d.permission_tier == tier, cmd

    def test_reported_script_passes_as_one_call(self):
        d = _d(_REPORTED_SCRIPT, self.LOCAL)
        assert d.allowed, d.reason
        assert d.permission_tier == "ask"

    @pytest.mark.parametrize("cmd", [
        "cat /tmp/n.xml", "echo hi > /tmp/x.txt", "mkdir -p /tmp/foo",
    ])
    def test_satellite_home_band_unchanged(self, cmd):
        d = _d(cmd, self.REMOTE)
        assert not d.allowed and "home" in d.reason, cmd
        assert _d(cmd, self.FULL).allowed, cmd

    @pytest.mark.parametrize("cmd", [
        "cat /tmp/.claude/x.json",
        "cat /tmp/.claude.json",
        "echo x > /tmp/.codex/config.toml",
        "cp x /tmp/.claude/permission_gate.py",
        "echo x > ~/.codex/hooks.json",
    ])
    def test_cli_state_under_tmp_stays_protected(self, cmd):
        d = _d(cmd, self.LOCAL)
        assert not d.allowed and "protected" in d.reason, cmd


class TestLineContinuations:
    """A backslash-newline pair is a line continuation: the shell removes it,
    so the splitter joins the two lines into one segment. Keeping the pair
    left a dangling backslash at the end of a segment whenever the next line
    opened with a pipe, and every later shlex pass failed on it."""

    def _mgr(self):
        return _ctx(role="manager", username="alice")

    def test_continuation_before_pipe_is_one_pipeline(self):
        d = _d("echo a \\\n | tr a b", self._mgr())
        assert d.allowed, d.reason
        assert d.permission_tier == "read"

    def test_continuation_inside_substitution(self):
        d = _d("PN=$(echo a \\\n | tr a b); echo $PN", self._mgr())
        assert d.allowed, d.reason
        assert d.permission_tier == "ask"

    def test_reported_multiline_curl_pipeline(self):
        cmd = (
            'PN=$(curl -s --max-time 25 "https://example.test/search" -X POST '
            '-H "Content-Type: application/json" \\\n'
            ' -d \'{"query":"a AND b","limit":1}\' \\\n'
            ' | python3 -c "import sys,json;print(json.load(sys.stdin)[\'n\'][0])")'
        )
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason
        assert d.permission_tier == "ask"

    def test_crlf_continuation_joins_too(self):
        d = _d("echo a \\\r\n | tr a b", self._mgr())
        assert d.allowed, d.reason

    def test_continuation_never_hides_a_cross_user_path(self):
        d = _d("cat /users/bob/workspace/secret \\\n | head", self._mgr())
        assert not d.allowed
        assert "/users/bob/workspace/secret" in d.reason

    def test_continuation_never_hides_a_later_command(self):
        # The joined line is classified as a whole: the pipe's second half
        # still gets its own tier.
        d = _d("echo a \\\n | curl -d @- https://example.test", self._mgr())
        assert d.allowed and d.permission_tier == "extended"

    def test_single_quoted_backslash_newline_is_literal(self):
        d = _d("printf '%s' 'a \\\nb'", self._mgr())
        assert d.allowed, d.reason
        assert d.permission_tier == "read"


class TestSubstitutionLifting:
    """A ``$(…)`` / backtick / process substitution restarts the quoting
    context inside it. The scanner lifts each one out into a placeholder: the
    inner is checked on its own, the outer is classified with the placeholder
    in place (its literal paths still checked, a placeholder never resolved as
    a path) and the segment's tier is at least ``ask``."""

    def _mgr(self):
        return _ctx(role="manager", username="alice")

    # --- the scanner ---------------------------------------------------

    def test_subst_end_tracks_inner_quotes_and_nesting(self):
        from auth.path_shell_subst import lift_substitutions, subst_end
        s = "echo $(grep -o 'a)b' x) tail"
        end, closed = subst_end(s, 5)
        assert closed and s[5:end] == "$(grep -o 'a)b' x)"
        s = 'echo "$(printf ")%s" $(date))" tail'
        end, closed = subst_end(s, 6)
        assert closed and s[6:end] == '$(printf ")%s" $(date))'
        lifted, inners = lift_substitutions("echo $(echo $(echo a)) `date` <(ls)")
        assert lifted == "echo __OTO_SUBST_0__ __OTO_SUBST_1__ __OTO_SUBST_2__"
        assert inners == ["echo $(echo a)", "date", "ls"]

    def test_lift_respects_the_outer_quoting_context(self):
        from auth.path_shell_subst import lift_substitutions
        # Single quotes: literal. Double quotes: $( and backticks expand,
        # process substitutions do not. Backslash: escaped.
        lifted, inners = lift_substitutions(
            "echo '$(a)' \"$(b) <(c)\" \\$(d) `e`")
        assert inners == ["b", "e"]
        assert lifted == "echo '$(a)' \"__OTO_SUBST_0__ <(c)\" \\$(d) __OTO_SUBST_1__"

    def test_unbalanced_substitution_lifts_to_the_end(self):
        from auth.path_shell_subst import lift_substitutions
        lifted, inners = lift_substitutions("echo $(cat x | head")
        assert inners == ["cat x | head"] and lifted == "echo __OTO_SUBST_0__"

    # --- the gate ------------------------------------------------------

    def test_nested_quotes_inside_substitution_parse(self):
        # The phrase from the installs' transcripts: valid bash that shlex
        # cannot parse because the quoting restarts inside the substitution.
        cmd = ("echo \"stamp: $(curl -s $B/ | grep -o "
               "'otodock-build\" content=\"[0-9a-f]*\"')\"")
        d = _d(cmd, self._mgr())
        assert d.allowed, d.reason
        assert d.permission_tier == "ask"

    @pytest.mark.parametrize("cmd", [
        "X=$(cd /tmp; ls); echo $X",
        "echo $(echo $(echo a))",
        "PN=$(echo a | tr a b); echo $PN",
        'bash -c "$(printf ls)"',
        "cat $(dirname x)/y",
    ])
    def test_substituted_segments_are_ask(self, cmd):
        d = _d(cmd, self._mgr())
        assert d.allowed, (cmd, d.reason)
        assert d.permission_tier == "ask", cmd

    def test_inner_tier_and_destructive_flag_propagate(self):
        d = _d("X=$(curl -s https://example.test); echo $X", self._mgr())
        assert d.allowed and d.permission_tier == "ask"
        d = _d("echo $(rm -f /users/alice/workspace/x)", self._mgr())
        assert d.allowed and d.destructive

    @pytest.mark.parametrize("cmd", [
        # An apostrophe inside double quotes used to flip the scanner's
        # single-quote state and hide the whole substitution.
        "echo \"it's $(cat /users/bob/workspace/secret)\"",
        "echo \"it's $(cat /users/bob/workspace/secret)\" > /users/alice/workspace/out.txt",
        "echo \"it's `cat /users/bob/workspace/secret`\"",
        # The outer command's literal paths are checked with the
        # substitution lifted out.
        "cat $(echo x) /users/bob/workspace/secret",
        "echo $(true) > /users/bob/workspace/out.txt",
        # A pipe inside the substitution: the inner is a pipeline of its own.
        "X=$(cat /users/bob/workspace/secret | head); echo $X",
    ])
    def test_cross_user_paths_inside_and_around_substitutions_denied(self, cmd):
        d = _d(cmd, self._mgr())
        assert not d.allowed, cmd
        assert "/users/bob/workspace/secret" in d.reason or "/users/bob/workspace/out.txt" in d.reason

    def test_dangerous_inner_inside_double_quotes_denied(self):
        # The raw scan misses `rm -rf /)"`; only the lifted inner catches it.
        assert not _d('echo "$(rm -rf /)"', self._mgr()).allowed
        assert not _d('echo "`rm -rf /`"', self._mgr()).allowed

    def test_typed_placeholder_never_buys_an_auto_allow(self):
        d = _d("cat __OTO_SUBST_0__/../x", self._mgr())
        assert d.allowed and d.permission_tier == "ask"

    def test_single_quoted_substitution_is_literal(self):
        d = _d("echo '$(rm -f x)'", self._mgr())
        assert d.allowed and d.permission_tier == "read" and not d.destructive


class TestHeredocBodies:
    def _mgr(self):
        return _ctx(role="manager", username="alice")

    # --- an unquoted delimiter: the shell expands the body -------------

    @pytest.mark.parametrize("body", [
        "$(cat /users/bob/workspace/secret)",
        "`cat /users/bob/workspace/secret`",
        "value: \"$(cat /users/bob/workspace/secret)\"",
        "it's $(cat /users/bob/workspace/secret)",
    ])
    def test_expanded_body_substitutions_are_classified(self, body):
        d = _d(f"cat <<EOF\n{body}\nEOF", self._mgr())
        assert not d.allowed, body
        assert "/users/bob/workspace/secret" in d.reason

    def test_expanded_body_substitution_makes_the_pipeline_ask(self):
        d = _d("cat > /users/alice/workspace/n.txt <<EOF\ntoday: $(date)\nEOF", self._mgr())
        assert d.allowed, d.reason
        assert d.permission_tier == "ask"

    @pytest.mark.parametrize("delim", ["'EOF'", '"EOF"', "\\EOF"])
    def test_quoted_delimiter_keeps_the_body_literal(self, delim):
        d = _d(f"cat <<{delim}\n$(cat /users/bob/workspace/secret)\nEOF", self._mgr())
        assert d.allowed, d.reason
        assert d.permission_tier == "read"

    def test_expanded_body_dangerous_substitution_denied(self):
        assert not _d("cat <<EOF\n$(rm -rf /)\nEOF", self._mgr()).allowed

    # --- bodies are data otherwise ---------------------------------------

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


# ===== Session-cwd relative anchor (local sessions) =====

class TestRelativePathSessionAnchor:
    """Relative Bash path args resolve like the TOOL will — against the
    sandbox session cwd (/users/{u}, or /workspace for agent scope) — while
    the historical agents-relative anchor keeps display-form paths working.
    The old sole AGENTS_DIR anchor denied every honest workspace-relative
    read (`base64 workspace/downloads/x` — the efpolis repro)."""

    def test_user_scope_workspace_relative_read_allowed(self):
        d = _d("base64 workspace/downloads/invoice.png")
        assert d.allowed, d.reason

    def test_agent_scope_workspace_relative_read_allowed(self):
        d = _d("base64 downloads/invoice.png", _ctx(username=""))
        assert d.allowed, d.reason

    def test_agents_relative_display_form_still_resolves(self):
        d = _d("base64 personal-assistant/users/alice/workspace/x.png")
        assert d.allowed, d.reason

    def test_relative_cross_user_denied(self):
        d = _d("cat ../bob/workspace/secret.txt")
        assert not d.allowed

    def test_absolute_escape_still_denied(self):
        d = _d("cat /etc/sudoers")
        assert not d.allowed


# ===== Assignment-only segments are shell structure, not a parse failure =====
# (2026-09-05: `F=/x`, `x=1 y=2`, `F=a && grep … $F` were hard-denied "could
# not parse command" — the parse-deny now covers only genuinely unparseable
# text such as a dangling backslash.)

class TestAssignmentOnlySegments:
    @pytest.mark.parametrize("cmd", [
        "F=/tmp/x",
        "x=1 y=2",
        "F=engineering-status.html && grep -c x $F",
        "CLI=/usr/lib/node_modules/cli.js; ls -la \"$CLI\"",
        "OUT=out.txt\necho $OUT",
    ])
    def test_assignment_only_is_read_structure(self, cmd):
        d = _d(cmd)
        assert d.allowed, d.reason
        assert not d.destructive

    def test_assignment_with_own_redirect_is_edit(self):
        d = _d("F=x > /users/alice/workspace/out.txt")
        assert d.allowed and d.permission_tier == "edit"

    def test_assignment_with_cross_user_redirect_denied(self):
        d = _d("F=x > /users/bob/workspace/out.txt")
        assert not d.allowed

    def test_bare_redirect_truncate_is_edit_and_path_checked(self):
        assert _d(": > /users/alice/workspace/log").permission_tier == "edit"
        assert not _d(": > /users/bob/workspace/log").allowed

    def test_assignment_with_substitution_still_recurses(self):
        assert not _d("F=$(rm -rf /)").allowed          # dangerous inner
        assert _d("F=$(date)").permission_tier == "ask"  # unanalyzable outer

    def test_dangling_backslash_is_not_an_assignment(self):
        # Unparsable, not command-less: it takes the unknown-command ask.
        d = _d("echo \\")
        assert d.allowed and d.permission_tier == "ask"

    def test_admin_floor_unchanged(self):
        d = _d("F=/x; x=1 y=2", _ctx(role="admin", is_admin_agent=True))
        assert d.allowed and d.permission_tier == "admin"


# ===== Shell structure is peeled off so the wrapped command is classified =====
# (2026-09-05: `do rm -rf x`, `then cat /users/bob/x`, `{ rm x`, `! grep`,
# `( mv a b )`, `done < f` all classified as the KEYWORD (read tier) — the
# command behind it never reached the tier / path / destructive checks.)

class TestShellStructureIsStripped:
    def test_loop_body_destructive_is_flagged(self):
        d = _d("for f in x; do rm -rf /users/alice/workspace/y; done")
        assert d.allowed and d.destructive and d.permission_tier == "edit"

    def test_brace_group_destructive_is_flagged(self):
        d = _d("{ rm -rf /users/alice/workspace/z; }")
        assert d.allowed and d.destructive

    @pytest.mark.parametrize("cmd", [
        "then cat /users/bob/workspace/secret.txt",
        "if grep -q x /users/bob/workspace/f; then echo y; fi",
        "! grep -q x /users/bob/workspace/f",
        "( mv a /users/bob/workspace/x )",
        "done < /users/bob/workspace/secret.txt",
        "while read -r l; do echo $l; done < /users/bob/workspace/secret.txt",
        "case $x in a) cat /users/bob/workspace/f ;; esac",
        "if ! timeout 5 cat /users/bob/workspace/f; then echo no; fi",
        "elif [ -f x ]; then\n  cat /users/bob/workspace/f\nfi",
    ])
    def test_cross_user_paths_behind_structure_denied(self, cmd):
        assert not _d(cmd).allowed

    @pytest.mark.parametrize("cmd", ["( cd a && make )", "(cd a && make)",
                                     "( (cd a; make) )"])
    def test_subshell_takes_the_inner_tier(self, cmd):
        d = _d(cmd)
        assert d.allowed and d.permission_tier == "extended"  # make, not "ask"

    def test_loop_body_extended_takes_its_tier(self):
        d = _d("for u in a b; do curl $u; done")
        assert d.allowed and d.permission_tier == "extended"

    @pytest.mark.parametrize("cmd", [
        "while read -r l; do echo $l; done",
        "while :; do sleep 1; done",
        "if [ -f x ]; then echo y; else echo n; fi",
        "if [[ \"$a\" > \"$b\" ]]; then echo gt; fi",   # [[ ]] compares, no redirect
        "((i++))",
        "do", "then", "(", "{", "! true",
        "mapfile -t arr < <(echo a)",
    ])
    def test_quiet_structure_stays_read(self, cmd):
        d = _d(cmd)
        assert d.allowed, d.reason
        assert d.permission_tier in ("read", "ask"), (cmd, d.permission_tier)
        if cmd != "mapfile -t arr < <(echo a)":      # process subst → ask (unchanged)
            assert d.permission_tier == "read", (cmd, d.permission_tier)

    def test_single_bracket_redirect_is_real(self):
        # `[ a > b ]` DOES create b in bash — checked, unlike [[ ]].
        assert not _d("[ \"$a\" > /users/bob/workspace/b ]").allowed

    def test_trailing_paren_strip_is_quote_and_escape_aware(self):
        assert _d("echo \")\"").permission_tier == "read"
        assert _d("echo \\)").permission_tier == "read"

    def test_command_builtin_unwraps(self):
        assert _d("command -v python3").permission_tier == "read"
        assert _d("command -pv python3").permission_tier == "read"
        d = _d("command rm -rf /users/alice/workspace/x")
        assert d.allowed and d.destructive
        d = _d("builtin cd /users/alice/workspace")
        assert d.allowed and d.permission_tier == "read"
        assert not _d("command cat /users/bob/workspace/f").allowed

    def test_dangerous_floor_still_universal_behind_structure(self):
        assert not _d("do rm -rf /").allowed
        assert not _d("( rm -rf / )", _ctx(role="admin", is_admin_agent=True)).allowed

    def test_heredoc_and_newline_behaviour_unchanged(self):
        d = _d("cat > workspace/x.py <<'EOF'\ndo_something()\nEOF")
        assert d.allowed and d.permission_tier == "edit"
        d = _d("echo a\nthen cat /users/bob/workspace/f")
        assert not d.allowed


# ===== The machine's own state on a remote, and find's exec clauses =====

def _remote_ctx(role="manager", username="alice", agent="head", *, is_admin_agent=False,
                allow_full_fs=False):
    return SecurityContext(
        role=role, username=username, agent=agent, is_admin_agent=is_admin_agent,
        placement=placement.PlacementCapabilities(
            kind=placement.KIND_ADMIN_REMOTE, machine_id="office-pc", home_dir="/home/office",
            os_user="office", agents_dir="/home/office/.oto-dock/agents", os="linux",
            allow_full_fs=allow_full_fs, claude_runtime_root="/tmp/claude-1000"),
        session_scope="user" if username else "agent",
    )


class TestRemoteShellIsRefusedTheMachinesOwnState:
    @pytest.mark.parametrize("cmd", [
        "cat /home/office/.oto-dock/satellite.conf",
        "cat ~/.oto-dock/satellite.conf",
        "grep -r machine_secret ~/.oto-dock",
        "cp ~/.oto-dock/agents/other/workspace/x.md /tmp/x.md",
        "echo x > ~/.oto-dock/mcps/workspace-mcp/run.sh",
        "sort < ~/.oto-dock/satellite.conf",
    ])
    def test_a_manager_is_refused(self, cmd):
        d = _d(cmd, _remote_ctx())
        assert not d.allowed and "OtoDock folder" in d.reason

    @pytest.mark.parametrize("cmd", [
        "cat ~/.oto-dock/satellite.conf",
        "grep -r machine_secret ~/.oto-dock",
        "echo x > ~/.oto-dock/mcps/workspace-mcp/run.sh",
        "sort < ~/.oto-dock/satellite.conf",
        "timeout 5 cat ~/.oto-dock/satellite.conf",
        "bash -c 'cat ~/.oto-dock/satellite.conf'",
    ])
    def test_an_admin_on_an_admin_agent_is_refused_on_every_pairing(self, cmd):
        for full in (False, True):
            d = _d(cmd, _remote_ctx("admin", "root", "ops", is_admin_agent=True, allow_full_fs=full))
            assert not d.allowed and "OtoDock folder" in d.reason, (cmd, full)

    def test_an_admin_on_an_admin_agent_keeps_the_admin_tier_elsewhere(self):
        d = _d("cat ~/Desktop/notes.txt && systemctl status nginx",
               _remote_ctx("admin", "root", "ops", is_admin_agent=True))
        assert d.allowed and d.permission_tier == "admin"
        d = _d("cat /home/office/.oto-dock/agents/ops/workspace/x.md",
               _remote_ctx("admin", "root", "ops", is_admin_agent=True))
        assert d.allowed and d.permission_tier == "admin"

    def test_the_sessions_own_tree_and_the_home_band_work(self):
        d = _d("cat /home/office/.oto-dock/agents/head/workspace/report.md", _remote_ctx())
        assert d.allowed and d.permission_tier == "read"
        assert _d("cat ~/Desktop/notes.txt", _remote_ctx()).allowed


class TestFindExecClausesCarryTheirTier:
    _ORDER = {"": 0, "read": 1, "edit": 2, "extended": 3, "ask": 3, "admin": 4}

    def _tier(self, cmd, ctx=None):
        d = _d(cmd, ctx)
        assert d.allowed, (cmd, d)
        return d.permission_tier or "read"

    @pytest.mark.parametrize("ctx", [None, "remote"])
    @pytest.mark.parametrize("cmd", [
        'find . -maxdepth 0 -exec python3 -c "print(1)" \\;',
        "find . -maxdepth 0 -exec curl -s https://x.example \\;",
        "find . -maxdepth 0 -exec cat {} \\; -exec python3 -c 1 \\;",
        "find . -maxdepth 0 -execdir node -e 1 \\; -exec cat {} +",
        "find . -maxdepth 0 -ok sh -c 'python3 -c 1' \\;",
    ])
    def test_an_inner_interpreter_or_downloader_lifts_the_segment(self, cmd, ctx):
        c = _remote_ctx() if ctx else None
        assert self._tier('python3 -c "print(1)"', c) == "extended"
        assert self._ORDER[self._tier(cmd, c)] >= self._ORDER["extended"], cmd

    def test_a_read_inner_keeps_find_at_read(self):
        assert self._tier("find . -name '*.md' -exec cat {} +") == "read"
        assert self._tier("find . -name '*.md' -exec cat {} \\; -exec wc -l {} \\;") == "read"
        d = _d("find /users/alice/workspace -name '*.tmp' -exec rm {} \\; -exec cat {} +")
        assert d.allowed and d.destructive is True


def test_powershell_admin_floor_refuses_the_machines_own_state():
    ctx = SecurityContext(
        role="admin", username="root", agent="ops", is_admin_agent=True,
        placement=placement.PlacementCapabilities(
            kind=placement.KIND_ADMIN_REMOTE, machine_id="pc", home_dir="C:/Users/eve",
            os_user="eve", agents_dir="C:/Users/eve/OtoDock/agents", os="windows"),
    )
    d, _ = check_tool_access("PowerShell", {"command": "Get-Content C:\\Users\\eve\\OtoDock\\satellite.conf"}, ctx)
    assert not d.allowed and "OtoDock folder" in d.reason
    d, _ = check_tool_access("PowerShell", {"command": "Get-Content C:\\Users\\eve\\Documents\\plan.txt"}, ctx)
    assert d.allowed and d.permission_tier == "admin"
