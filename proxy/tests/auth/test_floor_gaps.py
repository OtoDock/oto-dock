"""The command floor reads the words the shell runs: a root written through
shell quoting, the recursive delete's flags written apart, and an awk
program that starts commands (``auth/path_shell_words.py``). The floor
corpus (``test_floor_corpus.py``) records every context's answer; these
tests name the rules."""

import pytest

from auth import path_policy, path_shell, roles
from auth.path_shell_words import (
    RootDelete,
    awk_runs_commands,
    rsync_remote_shells,
    words_view,
)
from tests.auth.test_floor_corpus import _matrix

_REFUSED = "Bash denied: Recursive force-delete of / or ~ is blocked"


# --- the words the shell runs ------------------------------------------------

@pytest.mark.parametrize("text, view", [
    ("rm -rf $'/'", "rm -rf /"),
    ('rm -rf $"/"', "rm -rf /"),
    ("rm -rf $'\\x2f'", "rm -rf /"),
    ("rm -rf $'\\057'", "rm -rf /"),
    ("rm -rf $'/\\0tail'", "rm -rf /"),
    ("rm -rf ''$'/'", "rm -rf /"),
    ('rm -rf ""/', "rm -rf /"),
    ("r''m '-rf' /", "rm -rf /"),
    ("rm -rf /{,}", "rm -rf / /"),
    ("rm -rf {/,}", "rm -rf /"),
    ("rm -rf {/,}{,}", "rm -rf / /"),
    ("cp {a,b}{1,2} d", "cp a1 a2 b1 b2 d"),
    ("rm -rf \\/", "rm -rf /"),
    ("echo 'rm -rf /'", "echo 'rm -rf /'"),
    ("git commit -m 'a b'", "git commit -m 'a b'"),
    ("ls \"*.py\"", "ls '*.py'"),
    ("cat $(echo x) \"$HOME\"", "cat '$(echo x)' '$HOME'"),
    ("mkdir -p src/{api,ui}", "mkdir -p src/api src/ui"),
    ("a=1; b && c | d > e 2>&1", "a=1; b && c | d > e 2>&1"),
    ("echo x # a 'comment", "echo x # a 'comment"),
])
def test_the_view_writes_each_word_as_its_value(text, view):
    assert words_view(text) == view


def test_an_incomplete_form_has_no_view():
    assert words_view("echo 'open") is None
    assert words_view("echo $'open") is None


@pytest.mark.parametrize("cmd", [
    "rm -rf $'/'", 'rm -rf $"/"', "rm -rf $'\\x2f'", "rm -rf $'\\057'",
    "rm -rf $'/\\0tail'", "rm -rf ''$'/'", 'rm -rf ""/', "r''m -rf /", "rm '-rf' /",
    "rm -rf /{,}", "rm -rf {/,}", 'rm -rf "~/"', "rm -r -f /", "rm -v -rf /",
    "rm -f -r ~", "rm --recursive -f ~", "rm --rec --force /", "rm -i -rf ~", "rm / -rf",
    "rm -rf -- /", "( rm -r -f / )", "bash -c \"rm -rf $'/'\"", "echo x; rm -r -f ~",
    "rm -rf {/,}{,}", "rm -rf --no-preserve-root //", "rm -rf //",
    "rm -rf /./*", "rm -rf '//'",
])
def test_the_root_however_written_is_refused_in_every_context(cmd):
    for name, ctx in _matrix().items():
        d = path_shell._check_bash(cmd, ctx)
        assert not d.allowed and d.reason == _REFUSED, (name, d)


@pytest.mark.parametrize("text", [
    "rm -rf build/", "rm -r /tmp/x", "rm -f /", "rm -rf ./~", "rm -rf /tmp/*.log",
    "echo rm -rf", "git rm -r docs", "cat <<EOF\nrm -rf /\nEOF",
    "rm -rf build/../dist", "rm -rf /tmp/x/..", "rm -rf ./build", "rm -rf {a,b}{/,}",
])
def test_the_recursive_delete_matcher_reads_only_the_root(text):
    assert not RootDelete().search(text)


def test_an_undeclared_tool_argument_is_read_as_words_too():
    d = path_policy._unknown_tool_dangerous_scan({"note": "rm -rf $'/'"})
    assert not d.allowed and "Recursive force-delete" in d.reason
    assert path_policy._unknown_tool_dangerous_scan({"note": "it's fine"}).allowed


# --- awk ---------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "awk 'BEGIN{system(\"date\")}'",
    "awk '{print | \"sort\"}' f.txt",
    "awk '{print |& \"cat\"}' f.txt",
    "awk 'BEGIN{\"date\" | getline d; print d}'",
    "awk 'BEGIN { while ((getline line < \"x.md\") > 0) print line }'",
    "awk '{i++ / 2; print | \"cat\"}' f.txt",
    "awk '@load \"filefuncs\"; BEGIN{print 1}'",
    "awk -f prog.awk f.txt", "awk -fprog.awk f.txt", "awk --fil=prog.awk f.txt",
    "awk --file prog.awk f.txt", "awk -E prog.awk", "awk --exe prog.awk",
    "awk -i lib.awk '{print}' f.txt", "awk -l filefuncs 'BEGIN{}'", "awk --load=x 'BEGIN{}'",
    "awk -W exec prog.awk", "awk --dump-variables=v.txt 'BEGIN{}'", "awk -ov.txt '{print}'",
    "awk -e 'BEGIN{system(\"date\")}'", "awk --source='{print | \"sh\"}'",
    "LC_ALL=C awk '{print | \"sort\"}' f.txt",
    # a getopt cluster: the letters before the value-taking one are switches
    "awk -bf p.awk data.txt", "awk -bl ordchr 'BEGIN{}'", "awk -bi lib.awk '{print}' f.txt",
    "awk -cbE p.awk", "awk -bo '{print}'", "awk -be 'BEGIN{system(\"date\")}'",
    # gawk's -W name=value is the long option
    "awk -W file=p.awk f.txt", "awk -Wsource='{print | \"sh\"}'", "awk -W load=ordchr 'BEGIN{}'",
    # gawk's indirect call
    "awk 'BEGIN{f=\"sys\" \"tem\"; @f(\"id\")}'", "awk '{ @handler ($0) }' f.txt",
])
def test_an_awk_program_that_can_start_a_command_reads_as_extended(cmd):
    assert awk_runs_commands(cmd)
    ctx = _matrix()["editor"]
    assert path_shell._check_bash(cmd, ctx).permission_tier == "extended"


@pytest.mark.parametrize("cmd", [
    "awk '{print $1}' f.txt",
    "awk -F, -v OFS=';' '{print $1, $2}' f.txt",
    "awk '/a|b/ {print $1}' f.txt",
    "awk '{ if ($1 > 3 || $2 < 1) print }' f.txt",
    "awk '$3 > 100' f.txt",
    "awk 'BEGIN { x = 10 / 2; print x }'",
    "awk '/x/{n++} /y|z/{m++} END{print n, m}' f.txt",
    "awk -F'|' '{print $2}' f.txt",
    "awk '{print \"a|b\"}' f.txt",
    "awk 'NR > 1 { print $0 } # a | in a comment' f.txt",
    "awk -bv x=1 '{print x}' f.txt", "awk -bF, '{print $2}' f.txt", "awk -W posix '{print}' f.txt",
    "awk 'BEGIN{print \"me@example.com\"}'", "awk -b '{print}' f.txt",
])
def test_a_plain_awk_program_stays_read_tier(cmd):
    assert not awk_runs_commands(cmd)
    ctx = _matrix()["editor"]
    assert path_shell._check_bash(cmd, ctx).permission_tier == "read"


@pytest.mark.parametrize("cmd", [
    "awk --source='{print}' /users/bob/workspace/notes.txt",
    "awk -e '{print}' /users/bob/workspace/notes.txt",
    "awk --sourc '{print}' /users/bob/workspace/notes.txt",
])
def test_a_program_given_by_an_option_leaves_every_operand_a_checked_file(cmd):
    d = path_shell._check_bash(cmd, _matrix()["editor"])
    assert not d.allowed and "cannot read other users' files" in d.reason


# --- the destructive list, rsync and scp, pnpm 12, another account's home ---

@pytest.mark.parametrize("cmd", ["mkfs.ext4 /dev/sdz1", "mkfs.vfat /dev/sdz1",
                                 "mke2fs /dev/sdz1", "mkswap /dev/sdz2"])
def test_a_filesystem_builder_is_destructive(cmd):
    d = path_shell._check_bash(cmd, _matrix()["editor"])
    assert d.allowed and d.destructive and d.permission_tier == "ask"


@pytest.mark.parametrize("arg, remote", [
    ("host:/srv/x", True), ("user@host:", True), ("host::module/x", True),
    ("rsync://host/module", True), ("RSYNC://host/m", True), ("[::1]:/srv/x", True),
    ("user@[fe80::1]:/x", True), ("host:", True),
    ("/srv/x", False), ("./a:b", False), ("dir/a:b", False), (":x", False),
    ("C:", False), ("C:/Users/frank/x", False), ("C:\\Users\\x", False),
    ("[abc].txt", False), ("file[1]:x", True), ("x.md", False),
])
def test_a_remote_operand_is_named_exactly(arg, remote):
    assert path_shell.remote_operand(arg) is remote


_CLAUDE_DIR = "/workspace/" + ".claude"


@pytest.mark.parametrize("cmd, reads, writes", [
    ("rsync -a /workspace/ host:/srv/x", ["/workspace/"], []),
    ("rsync -a host:/srv/x /workspace/in/", [], ["/workspace/in/", "/workspace/in/x"]),
    (f"rsync host:/x/s.json {_CLAUDE_DIR}/", [], [f"{_CLAUDE_DIR}/", f"{_CLAUDE_DIR}/s.json"]),
    ("scp -P 2222 -o X=y /workspace/x.md host:/srv/", ["/workspace/x.md"], []),
    ("scp -i /home/k/id -F/home/k/cfg host:/x /workspace/",
     ["/home/k/id", "/home/k/cfg"], ["/workspace/", "/workspace/x"]),
    ("scp -3 h1:/a h2:/b", [], []),
    # an identity is judged for whose folder holds its .ssh, as ssh's is
    ("scp -i ~/.ssh/id_rsa f.txt host:", ["~", "f.txt"], []),
    ("scp -i/users/bob/.ssh/id f.txt host:", ["/users/bob", "f.txt"], []),
    # rsync's remote shell is a command (checked as one), never a path
    ("rsync -e 'ssh -i ~/.ssh/k' -a /workspace/x host:", ["/workspace/x"], []),
    ("rsync -avze ssh /workspace/x host:", ["/workspace/x"], []),
    ("rsync -ave'ssh -p 2' /workspace/x host:", ["/workspace/x"], []),
    ("rsync --rsh=ssh /workspace/x host:", ["/workspace/x"], []),
    ("rsync --rsh 'ssh -p 2' /workspace/x host:", ["/workspace/x"], []),
])
def test_rsync_and_scp_check_their_local_operands_only(cmd, reads, writes):
    name = cmd.split()[0]
    assert path_shell._extract_path_args(name, cmd) == (reads, writes)


@pytest.mark.parametrize("cmd", ["pn install", "pn add left-pad", "pnx create-vite app"])
def test_pnpm_12_short_names_are_pnpms_tier(cmd):
    assert path_shell._check_bash(cmd, _matrix()["editor"]).permission_tier == "extended"


@pytest.mark.parametrize("cmd", ["rm -rf ~nosuchuserxyz", "cat ~nosuchuserxyz/notes.md", "ls ~root"])
def test_another_accounts_home_is_refused_with_a_reason(cmd):
    for name, ctx in _matrix().items():
        if ctx.is_admin_agent and not ctx.read_only:
            continue
        d = path_shell._check_bash(cmd, ctx)
        assert not d.allowed and "another OS user's home directory" in d.reason, (name, d)


def test_an_unknown_account_never_raises_in_the_resolver():
    assert str(path_policy._resolve_path("~nosuchuserxyz/x")).endswith("~nosuchuserxyz/x")


def test_an_scp_identity_reads_as_ssh_s_does():
    """Your own key passes where ssh -i passes; another user's is refused."""
    for name, ctx in _matrix().items():
        own = path_shell._check_bash("scp -i ~/.ssh/id_rsa f.txt host:", ctx)
        ssh = path_shell._check_bash("ssh -i ~/.ssh/id_rsa host ls", ctx)
        assert own.allowed == ssh.allowed, name
        other = path_shell._check_bash("scp -i /users/bob/.ssh/id f.txt host:", ctx)
        assert not other.allowed or ctx.is_admin_agent or roles.is_admin(ctx.role), (name, other)


@pytest.mark.parametrize("segment, shells", [
    ("rsync -e 'ssh -p 2' a h:", ["ssh -p 2"]),
    ("rsync -avze 'nc h 22' a h:", ["nc h 22"]),
    ("rsync -aessh a h:", []),
    ("rsync --rsh=ssh a h:", []),
    ("rsync -aT /tmp/x -e 'sh -c x' a h:", ["sh -c x"]),
    ("rsync -Te a h:", []),
    ("RSYNC_RSH='nc h 22' rsync a h:", ["nc h 22"]),
    ("rsync -- -e x h:", []),
])
def test_rsync_names_its_remote_shell(segment, shells):
    assert rsync_remote_shells(segment) == shells


def test_rsync_s_remote_shell_takes_its_command_s_gate():
    """A named shell is checked as the command it is; a bare ssh is the
    shell rsync runs anyway."""
    for name, ctx in _matrix().items():
        shell = path_shell._check_bash("rsync -e 'ssh -i ~/.ssh/id_rsa' f.txt host:", ctx)
        ssh = path_shell._check_bash("ssh -i ~/.ssh/id_rsa host ls", ctx)
        assert shell.allowed == ssh.allowed, (name, shell)
        bare = path_shell._check_bash("rsync -avze ssh f.txt host:", ctx)
        plain = path_shell._check_bash("rsync -av f.txt host:", ctx)
        assert (bare.allowed, bare.permission_tier) == (plain.allowed, plain.permission_tier), name
        worst = path_shell._check_bash("rsync -e 'sh -c \"rm -rf ~\"' f.txt host:", ctx)
        assert not worst.allowed and worst.reason == _REFUSED, (name, worst)


# --- a NUL byte --------------------------------------------------------------

_NUL_FORMS = ["rm -rf /\x00x", 'rm -rf "/\x00x"', "rm -rf {/,x}\x00", "cat notes\x00.md"]


@pytest.mark.parametrize("cmd", _NUL_FORMS)
def test_a_command_holding_a_nul_is_refused_in_every_context(cmd):
    for name, ctx in _matrix().items():
        d = path_shell._check_bash(cmd, ctx)
        assert not d.allowed and "NUL byte" in d.reason, (name, d)
    d = path_shell._check_powershell("Get-Content a\x00b", _matrix()["editor"])
    assert not d.allowed and "NUL byte" in d.reason


def test_a_nul_never_raises_in_the_resolver_or_the_undeclared_tool_scan():
    assert "\x00" in str(path_policy._resolve_path("/tmp/a\x00b"))
    assert not path_policy._unknown_tool_dangerous_scan({"note": "a\x00b"}).allowed
