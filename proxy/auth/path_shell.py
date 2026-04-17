"""Shell command-execution gate: Bash, PowerShell, and WebFetch.

Classifies each command into a permission tier, extracts and RBAC-checks its
path arguments (cross-user / credential / agent-config backstops), and blocks
catastrophic forms outright. Driven by ``auth.path_policy.check_tool_access``;
the core data structures and path RBAC it builds on live in
``auth.path_policy``.
"""

import base64
import ipaddress
import re
import shlex
import urllib.parse

from auth.path_policy import (
    PathDecision,
    SecurityContext,
    _ALLOW,
    _check_read_path,
    _check_remote_bash_path,
    _check_write_path,
    _resolve_path,
    _translate_sandbox_path,
)

# Hostnames always treated as private (no DNS resolution needed)
_PRIVATE_HOSTNAMES = {"localhost"}
_PRIVATE_SUFFIXES = (".local", ".internal")


# ---------------------------------------------------------------------------
# Bash tool — tiered command security
# ---------------------------------------------------------------------------

# Tier "read": safe, read-only / inspection commands.
# Auto-approved in "default" mode (like Read/Glob/Grep). All roles.
_BASH_TIER_READ: set[str] = {
    "ls", "find", "cat", "head", "tail", "grep", "egrep", "fgrep",
    "wc", "diff", "stat", "file", "tree", "du", "df",
    "echo", "printf", "date", "pwd", "which", "whoami", "hostname", "id",
    "sort", "uniq", "cut", "tr", "awk", "sed", "jq", "column",
    "less", "more",
    "basename", "dirname", "realpath", "readlink",
    "md5sum", "sha256sum", "sha1sum",
    "test", "true", "false", "sleep", "seq", "expr", "bc",
    "cd",
    "rg", "nl", "tac",
    # `uv`/`uvx` (run arbitrary Python) and `xargs` (run an arbitrary command
    # per input line) are command RUNNERS, not readers — in the extended
    # tier so they prompt in default mode instead of auto-approving code
    # execution. Same bypass class as the deliberately-excluded `env`.
    # `printenv` is purpose-only (print env vars) — safe.
    # `env` is intentionally NOT in the allowlist: `env <cmd> <args>` runs
    # <cmd> with modified env, and our parser would classify the whole
    # invocation as `env` (read tier) — letting any admin-tier command
    # through. The `VAR=value <cmd>` form already covers legitimate
    # env-modified execution natively (parser skips KEY=value tokens).
    "printenv",
    # System / process / network INTROSPECTION (read-only, no file-path args of
    # concern) — expanded so the useful long tail auto-approves in default mode
    # instead of prompting. `env` is deliberately excluded (it's a wrapper that
    # runs another command — handled by the wrapper-unwrap, see _PREFIX_WRAPPERS).
    "ps", "pgrep", "pidof", "free", "vmstat", "uptime", "uname", "arch",
    "nproc", "getconf", "locale", "tty", "groups", "users", "who", "w",
    "lscpu", "lsblk", "lsusb", "lspci", "lsof", "getent", "cal", "type",
    "command", "ping", "ping6", "dig", "host", "nslookup",
    # File-content readers / transforms (their positional args ARE read paths —
    # also added to _READ_PATH_COMMANDS below so cross-user reads are gated).
    "base64", "base32", "xxd", "od", "hexdump", "strings",
    "cmp", "comm", "join", "paste", "fold", "fmt", "rev",
    # PDF inspection (poppler-utils, Tier 2 baseline).
    "pdftotext", "pdfinfo", "pdfimages", "pdftohtml",
}

# Tier "edit": file-modifying commands.
# Auto-approved in "acceptEdits" mode (like Write/Edit). All roles
# (paths still restricted by bwrap to the role's writable scope).
# `git`/`gh` are edit-tier: `git push` and `gh pr create` mutate remote
# state. Per-subcommand granularity (read/edit/admin inside git/gh)
# would need a deeper parser — out of scope for v1.
_BASH_TIER_EDIT: set[str] = {
    "rm", "cp", "mv", "mkdir", "rmdir", "touch",
    "tee", "tar", "zip", "unzip", "gzip", "gunzip",
    "bzip2", "bunzip2", "xz",
    "chmod", "ln", "patch", "rsync", "install",
    "git", "gh",
    # Document processing (poppler-utils + sqlite3, Tier 2 baseline).
    # Office / PDF / image editing go through file-tools-mcp.
    "pdfseparate", "pdfunite",              # poppler-utils write variants
    "sqlite3",                              # CLI is read-or-write depending on args
}

# Tier "extended": scripting + network fetch + builds + package install.
# Scoped by the bwrap sandbox to the agent's allowed paths. Available to
# ALL roles — the platform deliberately installs the dev toolchain
# (Python, Node, build tools, network utilities) so agents can do real
# development inside their sandbox. The bwrap kernel namespace is the
# security boundary: an agent running ``python3 evil.py`` can only
# touch the dirs its role mounts, can only network out (no SSRF), and
# cannot escape the sandbox. The bash allowlist gates command TYPES —
# role gates filesystem ACCESS via bwrap, not command runners.
_BASH_TIER_EXTENDED: set[str] = {
    # Network fetch. WebFetch has its own URL-level SSRF gate; raw
    # curl/wget are covered by the sandbox network namespace instead
    # (pasta --block-private blackholes cloud-metadata + private/LAN
    # ranges, so they can only reach public internet destinations).
    "curl", "wget",
    # Script runtimes — bounded by bwrap filesystem scope
    "python3", "python", "node", "deno",
    # Command runners (NOT read-tier): `uv run` / `uvx` execute arbitrary
    # Python; `xargs <cmd>` runs an arbitrary command. A read-tier
    # classification would auto-approve that code execution.
    "uv", "uvx", "xargs",
    # Package installers — write to scope-allowed dirs (./venv, ./node_modules)
    "pip", "pip3", "npm", "npx", "yarn", "pnpm",
    # Build tooling
    "make", "cmake", "go", "cargo", "gcc", "g++",
}

# Tier "admin": host-touching ops — containers, host services, remote SSH,
# system package installation. Admin role only.
#
# These would typically fail in a default sandbox anyway (docker has no
# socket mount, ssh has no key access, apt requires root), but we
# explicitly deny them at the role layer so the LLM gets a clear error
# instead of an opaque failure. Future work could allow ssh on
# admin-paired remote satellites where the operator has set up keys.
_BASH_TIER_ADMIN: set[str] = {
    "docker", "docker-compose", "podman",
    "systemctl", "journalctl",
    "ssh", "scp",
    "apt", "apt-get",
}

# Combined lookup: command name -> tier string
_BASH_COMMAND_TIER: dict[str, str] = {}
for _cmd in _BASH_TIER_READ:
    _BASH_COMMAND_TIER[_cmd] = "read"
for _cmd in _BASH_TIER_EDIT:
    _BASH_COMMAND_TIER[_cmd] = "edit"
for _cmd in _BASH_TIER_EXTENDED:
    _BASH_COMMAND_TIER[_cmd] = "extended"
for _cmd in _BASH_TIER_ADMIN:
    _BASH_COMMAND_TIER[_cmd] = "admin"

# Tier ordering for "max tier across segments"
# "ask" ties "extended": both prompt in default/acceptEdits and run in
# dontAsk/auto (Pass-2). The ordering only picks the pipeline's max for the
# prompt decision, so a read+ask pipeline → "ask" (prompts), extended+ask →
# stays extended (also prompts). Destructiveness is tracked separately
# (PathDecision.destructive), NOT as a tier level.
_TIER_ORDER: dict[str, int] = {"": 0, "read": 1, "edit": 2, "extended": 3, "ask": 3, "admin": 4}

# ---------------------------------------------------------------------------
# Dangerous pattern blocklist — deny regardless of role (except admin+admin_agent)
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Recursive force-delete at root or home
    (re.compile(
        r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r"
        r"|--recursive\s+--force|--force\s+--recursive)"
        r"\s+(/\s*$|/\s*[;&|]|/\*|~\s*$|~\s*[;&|])"
    ), "Recursive force-delete of / or ~ is blocked"),

    # Fork bombs
    (re.compile(r":\(\)\s*\{.*\|.*\}"), "Fork bomb detected"),

    # Direct device writes
    (re.compile(r">\s*/dev/(?:sd[a-z]|nvme|vd[a-z]|xvd[a-z])"), "Direct device write blocked"),
    (re.compile(r"\bdd\b.*\bof\s*=\s*/dev/"), "dd to device blocked"),

    # /proc and /sys writes
    (re.compile(r">\s*/proc/"), "Write to /proc blocked"),
    (re.compile(r">\s*/sys/"), "Write to /sys blocked"),

    # Kernel module operations
    (re.compile(r"\b(insmod|modprobe|rmmod)\b"), "Kernel module operations blocked"),

    # Bash network pseudo-devices
    (re.compile(r"/dev/tcp/"), "Bash /dev/tcp access blocked"),
    (re.compile(r"/dev/udp/"), "Bash /dev/udp access blocked"),

    # Sensitive file reads
    (re.compile(r"\bcat\b.*\b/etc/shadow\b"), "Reading /etc/shadow blocked"),
]

# ---------------------------------------------------------------------------
# Path extraction categories for Bash commands
# ---------------------------------------------------------------------------

# All positional args are read paths (value-flag values skipped)
_READ_PATH_COMMANDS: set[str] = {
    "cat", "head", "tail", "less", "more", "file", "stat",
    "wc", "md5sum", "sha256sum", "sha1sum",
    "diff", "tree", "du", "readlink", "ls",
    # Analyzable readers — so a casual `sort /users/OTHER/x` is path-checked.
    "sort", "uniq", "cut", "column", "nl", "tac",
    # Readers / transforms — positional args are read paths, so a
    # cross-user `base64 /users/OTHER/x` is gated like `cat`.
    "base64", "base32", "xxd", "od", "hexdump", "strings",
    "cmp", "comm", "join", "paste", "fold", "fmt", "rev",
    # `source FILE` / `. FILE` read + execute a script — gate the file arg as a
    # cross-user read (the execution itself is the interpreter residual, tier
    # "ask"). Only triggers when the segment STARTS with `source`/`.`.
    "source", ".",
}

# All positional args are write paths
_WRITE_PATH_COMMANDS: set[str] = {
    "rm", "rmdir", "touch", "mkdir",
}

# Source/dest: last positional = write, rest = read
_COPY_COMMANDS: set[str] = {"cp", "mv", "ln", "install", "rsync"}

# First positional is a pattern (not a path), rest are read paths
_GREP_COMMANDS: set[str] = {"grep", "egrep", "fgrep"}

# Commands with in-place flag that makes them writers
_INPLACE_FLAG_COMMANDS: dict[str, str] = {"sed": "-i"}

# Program/pattern-first readers: the first POSITIONAL is the program /
# pattern (awk script, jq filter, rg regex), the rest are read paths — UNLESS
# the program/pattern was supplied via a flag (see _PROGRAM_FLAGS_BY_CMD), in
# which case EVERY positional is a read path.
_PROGRAM_FIRST_READ_COMMANDS: set[str] = {"awk", "jq", "rg"}

# Flags that SUPPLY the program/pattern (so there is no inline positional one);
# their value is consumed, not treated as a path.
_PROGRAM_FLAGS_BY_CMD: dict[str, set[str]] = {
    "rg":  {"-e", "--regexp", "-f", "--file"},
    "awk": {"-f", "--file"},
    "jq":  {"-f", "--from-file"},
}

# Flags whose value is a WRITE path (vs the read-path positionals), e.g.
# `sort -o OUT in.txt` writes OUT.
_WRITE_VALUE_FLAGS_BY_CMD: dict[str, set[str]] = {
    "sort": {"-o", "--output"},
}

# Per-command flags that take a non-path value as the next token.
# Without this table, `head -c 8 file.txt` would parse `8` as a path
# (rejected because no file named "8" exists in the agent's scope), so a
# legitimate `head -c 8` env-introspection command would fail. The fused form
# (`--bytes=8`, `-c8`) is already handled by the existing "starts with
# `-`" skip; this table only needs the SEPARATED form (`-c 8`,
# `--bytes 8`). Add entries as new commands surface this issue.
_VALUE_FLAGS_BY_CMD: dict[str, set[str]] = {
    "head": {"-c", "-n", "--bytes", "--lines"},
    "tail": {"-c", "-n", "--bytes", "--lines"},
    "tree": {"-L", "-P", "-I"},
    "du":   {"-d", "-B", "--max-depth", "--block-size", "--threshold"},
    "stat": {"-c", "--format", "--printf"},
    "diff": {"-x", "--exclude", "-I", "--ignore-matching-lines"},
    "ls":   {"-w", "-T", "--width", "--tabsize", "--block-size"},
    # Readers (skip non-path flag values so e.g. `cut -f 1 file`, `sort -k 2
    # file`, `rg -A 3 PAT path` don't parse the value as a bogus path → deny).
    "sort": {"-k", "-t", "-S", "-T", "--key", "--field-separator",
             "--buffer-size", "--temporary-directory"},
    "uniq": {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"},
    "cut":  {"-d", "-f", "-c", "-b", "--delimiter", "--fields",
             "--characters", "--bytes", "--output-delimiter"},
    "column": {"-c", "-s", "-o", "--columns", "--separator",
               "--output-separator", "--table-columns"},
    "nl":   {"-b", "-d", "-f", "-h", "-i", "-l", "-n", "-s", "-v", "-w"},
    "tac":  {"-s", "--separator"},
    "awk":  {"-v", "-F", "--assign", "--field-separator"},
    "jq":   {"-L", "--indent", "--arg", "--argjson", "--slurpfile", "--rawfile"},
    "rg":   {"-A", "-B", "-C", "--after-context", "--before-context",
             "--context", "-m", "--max-count", "-g", "--glob", "-t", "--type",
             "-T", "--type-not", "--max-depth", "-M", "--max-columns", "-r",
             "--replace", "-E", "--encoding", "--color", "--colors"},
    # `find` handled separately (positional path BEFORE flags; existing
    # "break on first `-` token" rule covers its many value-flags
    # naturally).
}

# ---------------------------------------------------------------------------
# Command-policy v2 (cross-platform exec-env hardening)
# ---------------------------------------------------------------------------
# The bash gate is defense-in-depth, NOT a boundary: bwrap is the boundary
# locally; the PATH check is the only cross-user boundary on shared-admin
# satellites (and it's moot on local + user-paired). So:
#   * _DANGEROUS_PATTERNS is the ONLY hard-deny for non-admin (run at EVERY
#     recursion level, incl. unwrapped `bash -c` / command-substitution inners
#     — quotes/parens defeat a single raw-string scan, so we re-scan the inner).
#   * An unknown command → tier "ask" (prompt in default/acceptEdits, run in
#     dontAsk/auto) — never a hard-deny.
#   * Sub-shell wrappers + prefix wrappers are UNWRAPPED and the inner command
#     is re-checked; command-substitution `$(…)`/`` `…` ``/`<(…)` makes a
#     segment "ask" (unanalyzable) after its inner is recursively checked.

# Recursion cap for unwrap / command-substitution nesting (pathological input).
_MAX_BASH_DEPTH = 8

# Sub-shell wrappers whose ``-c``/``-lc``/``-ic`` argument is a full command
# STRING to recurse into. ``bash -c "rm -rf /"`` must be re-checked against the
# dangerous patterns on the INNER (the trailing quote defeats the raw scan).
_SHELL_DASH_C: set[str] = {"bash", "sh", "zsh", "dash", "ksh", "ash"}

# Prefix wrappers: ``<wrapper> [opts] <real-cmd> <args>`` runs <real-cmd>. We
# strip the wrapper + its leading option/value/duration/assignment tokens and
# re-check the inner command, so ``timeout 60 cat x`` classifies as the inner
# `cat` (read → auto) and ``timeout 60 rm x`` as destructive. Best-effort: an
# odd wrapper form that doesn't strip cleanly lands the inner on a non-command
# token → "ask" (prompts in default/acceptEdits) — never a wrong auto-allow,
# because _DANGEROUS_PATTERNS already ran on the full raw string.
_PREFIX_WRAPPERS: set[str] = {
    "timeout", "xargs", "nohup", "time", "stdbuf", "nice", "ionice", "setsid",
    "env", "exec",
}

# ``eval <args>`` executes the joined args as a command string — recurse on the
# inner so the dangerous scan re-runs UNQUOTED (``eval "rm -rf /"`` defeats the
# raw scan via the trailing quote, exactly like ``bash -c``).
_EVAL_CMDS: set[str] = {"eval"}

# Shell control-flow keywords + no-op builtins — STRUCTURAL, not "unknown
# commands". Without this, ``for f in *.py; do cat $f; done`` would classify
# `for`/`do`/`done` as unknown → spurious prompts. Treated as read-tier with no
# path args (they don't read file content themselves). NOTE: ``eval``/``exec``/
# ``source``/``.`` are deliberately EXCLUDED — they execute/read and are handled
# as wrappers / readers so the inner command + file args are still checked.
_SHELL_STRUCTURAL: set[str] = {
    "for", "while", "until", "if", "then", "else", "elif", "fi", "do", "done",
    "case", "esac", "select", "function", "in", "{", "}", "[[", "]]", "!", "[",
    ":", "cd", "export", "set", "unset", "shift", "return", "local", "declare",
    "typeset", "readonly", "pushd", "popd", "dirs", "alias", "unalias", "wait",
    "trap", "exit", "break", "continue", "let",
}

# Destructive commands — prompt EVEN in acceptEdits (PathDecision.destructive,
# a separate boolean, NOT a tier). Catastrophic forms (``rm -rf /``,
# ``dd of=/dev/…``) are ALSO hard-denied by _DANGEROUS_PATTERNS.
_DESTRUCTIVE_COMMANDS: set[str] = {
    "rm", "rmdir", "shred", "truncate", "dd", "mkfs", "wipefs", "blkdiscard",
    "fdisk", "sgdisk", "parted",
}

# Tokens that look like wrapper options/values/durations/assignments to skip
# while locating a prefix wrapper's inner command (best-effort — see above).
_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# ---------------------------------------------------------------------------
# Bash command parsing helpers
# ---------------------------------------------------------------------------


def _split_command_segments(command: str) -> list[str]:
    """Split a shell command on unquoted ;, &&, ||, |, and newlines into segments.

    Heredoc bodies (``cmd <<DELIM`` … ``DELIM``) are stdin DATA, not commands —
    they are consumed here and never become segments. Exception: when the
    receiving line names a shell (``bash <<EOF`` executes its stdin), body
    lines keep per-line classification so the dangerous floor still sees them.

    Returns a list of stripped, non-empty command strings.
    Raises ValueError on unclosed quotes.
    """
    segments: list[str] = []
    current: list[str] = []
    i = 0
    in_single = False
    in_double = False
    # Heredocs declared on the current line, in order: (delimiter, strip_tabs
    # for <<-, body-is-shell). Bodies start after the line's newline.
    pending_heredocs: list[tuple[str, bool, bool]] = []

    while i < len(command):
        ch = command[i]

        # Backslash escape (outside single quotes)
        if ch == "\\" and not in_single and i + 1 < len(command):
            current.append(ch)
            current.append(command[i + 1])
            i += 2
            continue

        # Quote tracking
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue

        # Inside quotes: consume literally
        if in_single or in_double:
            current.append(ch)
            i += 1
            continue

        # $((…)) arithmetic — consume atomically so `<<` inside (bit shift)
        # can't read as a heredoc operator and swallow the following lines.
        if ch == "$" and command[i:i + 3] == "$((":
            depth = 0
            k = i + 1
            while k < len(command):
                if command[k] == "(":
                    depth += 1
                elif command[k] == ")":
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
                k += 1
            current.append(command[i:k])
            i = k
            continue

        # <<< here-string: an inline word, not a heredoc — pass through.
        if ch == "<" and command[i:i + 3] == "<<<":
            current.append("<<<")
            i += 3
            continue

        # << / <<- heredoc operator: parse the delimiter and queue the body
        # for consumption at this line's newline.
        if ch == "<" and command[i:i + 2] == "<<":
            j = i + 2
            strip_tabs = False
            if j < len(command) and command[j] == "-":
                strip_tabs = True
                j += 1
            k = j
            while k < len(command) and command[k] in " \t":
                k += 1
            delim: str | None = None
            if k < len(command) and command[k] in "'\"":
                q = command[k]
                end = command.find(q, k + 1)
                if end != -1:
                    delim = command[k + 1:end]
                    k = end + 1
            else:
                m = k
                while m < len(command) and command[m] not in " \t\n\r;|&<>()`":
                    m += 1
                if m > k:
                    delim = command[k:m].replace("\\", "")
                    k = m
            # Digit/`$`-leading "delimiters" are almost certainly arithmetic
            # shifts in disguise (`((x<<2))`) — treat the operator literally
            # rather than risk swallowing the rest of the command as body.
            if delim and not delim[0].isdigit() and not delim.startswith("$"):
                line_tokens = "".join(current).split()
                body_is_shell = any(
                    t.rsplit("/", 1)[-1] in _SHELL_DASH_C for t in line_tokens
                )
                pending_heredocs.append((delim, strip_tabs, body_is_shell))
                current.append(command[i:k])
                i = k
                continue
            current.append("<<")
            i += 2
            continue

        # Unquoted separator detection
        # A newline / carriage-return is a statement separator: without this,
        # the later lines of a multi-line command inherit the FIRST line's
        # tier / path / role classification (shlex collapses \n to whitespace),
        # which is a gate bypass on remote / no-bwrap execution. Escaped
        # newlines (\<newline>) are consumed by the backslash handler above, so
        # genuine line continuations are preserved.
        if ch == "\n" or ch == "\r":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 1
            # Heredoc bodies follow this line: consume each queued one up to
            # its terminator line. Data bodies vanish; shell bodies keep
            # per-line classification (appended as segments).
            for delim, strip_tabs, body_is_shell in pending_heredocs:
                while i < len(command):
                    nl = command.find("\n", i)
                    line = command[i:nl] if nl != -1 else command[i:]
                    i = (nl + 1) if nl != -1 else len(command)
                    check = line.rstrip("\r")
                    if strip_tabs:
                        check = check.lstrip("\t")
                    if check == delim:
                        break
                    if body_is_shell:
                        body_seg = line.strip()
                        if body_seg:
                            segments.append(body_seg)
            pending_heredocs = []
            continue
        if ch == ";":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 1
            continue
        if ch == "&" and i + 1 < len(command) and command[i + 1] == "&":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 2
            continue
        if ch == "|" and i + 1 < len(command) and command[i + 1] == "|":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 2
            continue
        if ch == "|" and i + 1 < len(command) and command[i + 1] == "&":
            # `|&` — pipe both stdout+stderr (bash). Split like a plain pipe so
            # the second segment isn't left with a stray leading `&`.
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 2
            continue
        if ch == "|":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    if in_single or in_double:
        raise ValueError("Unclosed quote in command")

    seg = "".join(current).strip()
    if seg:
        segments.append(seg)

    return segments


def _extract_command_name(segment: str) -> str | None:
    """Extract the base command name from a single command segment.

    Skips leading env var assignments (KEY=value).
    Strips path prefix (/usr/bin/ls -> ls).
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None

    for token in tokens:
        # Skip env var assignments (KEY=value)
        if "=" in token and not token.startswith("-"):
            eq_pos = token.index("=")
            key = token[:eq_pos]
            if key.isidentifier():
                continue
        # Strip path prefix (/usr/bin/ls -> ls; C:\Windows\cmd.exe -> cmd.exe;
        # also un-aliases a leading `\rm`) + a Windows ``.exe`` suffix, so a
        # residual ``.exe`` invocation that reaches the Bash checker (rather than
        # the PowerShell route) classifies on its bare name.
        name = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if name.lower().endswith(".exe"):
            name = name[:-4]
        return name

    return None


def _extract_redirect_targets(segment: str) -> list[str]:
    """Extract file paths from >, >>, 2>, 2>> redirect operators.

    Skips fd redirects (>&, 2>&1). Returns raw path strings.
    """
    targets: list[str] = []
    i = 0
    in_single = False
    in_double = False

    while i < len(segment):
        ch = segment[i]

        if ch == "\\" and not in_single and i + 1 < len(segment):
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if in_single or in_double:
            i += 1
            continue

        # Detect redirect operators
        if ch == ">":
            # Check if preceded by a digit (stderr redirect 2>)
            # Skip fd-to-fd redirects: >&, 2>&1
            next_i = i + 1
            if next_i < len(segment) and segment[next_i] == ">":
                next_i += 1  # >>
            if next_i < len(segment) and segment[next_i] == "&":
                i = next_i + 1  # skip >&N or >>&N
                continue

            # Move past the operator
            i = next_i
            # Skip whitespace
            while i < len(segment) and segment[i] == " ":
                i += 1
            # Collect target path
            path_chars: list[str] = []
            while i < len(segment) and segment[i] not in (" ", ";", "&", "|", "\n"):
                path_chars.append(segment[i])
                i += 1
            if path_chars:
                raw = "".join(path_chars).strip("'\"")
                targets.append(raw)
            continue

        i += 1

    return targets


def _filter_redirect_tokens(args: list[str]) -> list[str]:
    """Remove redirect operators and their targets from an args list.

    shlex.split keeps redirects as tokens, e.g.:
      "ls 2> /dev/null"   → ['ls', '2>', '/dev/null']
      "echo hi > out.txt" → ['echo', 'hi', '>', 'out.txt']
    These must be stripped before path-arg extraction.
    """
    filtered: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        # Standalone redirect operators (output > and input <). Input targets
        # are separately read-checked by _extract_input_redirects; here we just
        # keep them out of the positional path args. A spaced heredoc operator
        # (`<< EOF` / `<< 'EOF'`) consumes its delimiter the same way — the
        # delimiter is a sentinel, not a path.
        if arg in (">", ">>", "2>", "2>>", "1>", "1>>", "<", "0<", "<<", "<<-"):
            skip_next = True  # next token is the target / heredoc delimiter
            continue
        # Fused redirect: >/path, >>/path, 2>/path, </path, 0</path, and the
        # heredoc sentinel <<EOF (so it isn't parsed as a bogus path).
        if re.match(r"^[012]?>>?", arg) or re.match(r"^[0-9]*<", arg):
            continue
        filtered.append(arg)
    return filtered


def _extract_path_args(
    cmd_name: str, segment: str
) -> tuple[list[str], list[str]]:
    """Extract (read_paths, write_paths) from a command segment.

    Returns lists of raw path strings needing validation.
    Does NOT extract redirect targets (handled separately).
    """
    read_paths: list[str] = []
    write_paths: list[str] = []

    try:
        tokens = shlex.split(segment)
    except ValueError:
        return [], []

    if not tokens:
        return [], []

    # Skip past env var assignments and the command name itself
    args: list[str] = []
    found_cmd = False
    for token in tokens:
        if not found_cmd:
            if "=" in token and not token.startswith("-"):
                eq_pos = token.index("=")
                if token[:eq_pos].isidentifier():
                    continue
            found_cmd = True
            continue  # skip the command name
        args.append(token)

    # Strip redirect operators and their targets from args
    args = _filter_redirect_tokens(args)

    # --- Command-specific path extraction ---

    if cmd_name in _READ_PATH_COMMANDS:
        # Skip the token AFTER a flag that takes a non-path value (e.g.
        # the `8` in `head -c 8 file.txt`). Without this, the `8` gets
        # path-validated and the command is denied. See
        # _VALUE_FLAGS_BY_CMD docstring. A WRITE-value flag (`sort -o FILE`)
        # routes its value to write_paths instead of read_paths.
        value_flags = _VALUE_FLAGS_BY_CMD.get(cmd_name, set())
        write_value_flags = _WRITE_VALUE_FLAGS_BY_CMD.get(cmd_name, set())
        skip_next = False
        write_next = False
        for arg in args:
            if write_next:
                write_next = False
                write_paths.append(arg)
                continue
            if skip_next:
                skip_next = False
                continue
            if arg in write_value_flags:
                write_next = True
                continue
            if arg in value_flags:
                skip_next = True
                continue
            if not arg.startswith("-"):
                read_paths.append(arg)

    elif cmd_name in _GREP_COMMANDS:
        # First positional is the pattern (skip), rest are file paths
        positional = [a for a in args if not a.startswith("-")]
        for p in positional[1:]:
            read_paths.append(p)

    elif cmd_name in _PROGRAM_FIRST_READ_COMMANDS:
        # awk/jq/rg: skip value-flag values; the first POSITIONAL is the
        # program/pattern UNLESS supplied via a flag (-e/-f), in which case
        # EVERY positional is a read path. (Without value-flag skipping,
        # `rg -A 3 PAT /path` would parse `3`/`PAT` as files and falsely deny;
        # without the flag check, `rg -e PAT /path` would drop `/path`.)
        value_flags = _VALUE_FLAGS_BY_CMD.get(cmd_name, set())
        program_flags = _PROGRAM_FLAGS_BY_CMD.get(cmd_name, set())
        positional: list[str] = []
        skip_next = False
        pattern_from_flag = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in program_flags:
                pattern_from_flag = True
                skip_next = True  # its value is the pattern/script, not a path
                continue
            if arg in value_flags:
                skip_next = True
                continue
            if not arg.startswith("-"):
                positional.append(arg)
        files = positional if pattern_from_flag else positional[1:]
        read_paths.extend(files)

    elif cmd_name == "find":
        # First positional arg(s) before any -flag are search paths
        for arg in args:
            if arg.startswith("-"):
                break
            read_paths.append(arg)

    elif cmd_name in _WRITE_PATH_COMMANDS:
        for arg in args:
            if not arg.startswith("-"):
                write_paths.append(arg)

    elif cmd_name in _COPY_COMMANDS:
        # Last non-flag = destination (write), rest = sources (read)
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) >= 2:
            for p in positional[:-1]:
                read_paths.append(p)
            write_paths.append(positional[-1])
        elif len(positional) == 1:
            write_paths.append(positional[0])

    elif cmd_name in _INPLACE_FLAG_COMMANDS:
        flag = _INPLACE_FLAG_COMMANDS[cmd_name]
        has_inplace = any(a == flag or a.startswith(flag) for a in args)
        # First positional is the script/pattern (not a path), rest are files
        positional = [a for a in args if not a.startswith("-")]
        file_args = positional[1:]  # skip script arg
        if has_inplace:
            for p in file_args:
                write_paths.append(p)
        else:
            for p in file_args:
                read_paths.append(p)

    elif cmd_name == "tee":
        for arg in args:
            if not arg.startswith("-"):
                write_paths.append(arg)

    elif cmd_name in ("chmod", "chown", "chgrp"):
        # First positional is mode/owner, rest are target paths
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) >= 2:
            for p in positional[1:]:
                write_paths.append(p)

    elif cmd_name == "tar":
        for i, arg in enumerate(args):
            if arg in ("-C", "--directory") and i + 1 < len(args):
                write_paths.append(args[i + 1])
            elif arg in ("-f", "--file") and i + 1 < len(args):
                read_paths.append(args[i + 1])

    return read_paths, write_paths


def _extract_substitution_inners(segment: str) -> tuple[list[str], bool]:
    """Find command-substitution inners: ``$(…)`` / `` `…` `` / ``<(…)`` /
    ``>(…)``. Returns (inner command strings, found_any). Single-quoted spans
    are literal (skipped). Paren-balanced so nested ``$( … $(…) … )`` returns
    the full outer inner (re-scanned on recursion). ``${VAR}`` is var expansion,
    NOT a command substitution — ignored."""
    inners: list[str] = []
    found = False
    i, n = 0, len(segment)
    in_single = False
    while i < n:
        ch = segment[i]
        if ch == "\\" and not in_single and i + 1 < n:
            i += 2
            continue
        if ch == "'":
            in_single = not in_single
            i += 1
            continue
        if in_single:
            i += 1
            continue
        if segment[i:i + 2] in ("$(", "<(", ">("):
            depth = 0
            j = i + 1  # points at '('
            start = i + 2
            while j < n:
                if segment[j] == "(":
                    depth += 1
                elif segment[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inners.append(segment[start:j])
            found = True
            i = j + 1
            continue
        if ch == "`":
            j = segment.find("`", i + 1)
            if j == -1:
                break
            inners.append(segment[i + 1:j])
            found = True
            i = j + 1
            continue
        i += 1
    return inners, found


def _drop_leading_tokens(s: str, count: int) -> str:
    """Return ``s`` with the first ``count`` whitespace-delimited (quote-aware)
    tokens removed, preserving the remainder VERBATIM (quotes + redirects)."""
    i, n, dropped = 0, len(s), 0
    while dropped < count and i < n:
        while i < n and s[i].isspace():
            i += 1
        in_s = in_d = False
        while i < n:
            c = s[i]
            if c == "\\" and not in_s and i + 1 < n:
                i += 2
                continue
            if c == "'" and not in_d:
                in_s = not in_s
                i += 1
                continue
            if c == '"' and not in_s:
                in_d = not in_d
                i += 1
                continue
            if not in_s and not in_d and c.isspace():
                break
            i += 1
        dropped += 1
    while i < n and s[i].isspace():
        i += 1
    return s[i:]


def _unwrap_segment(segment: str) -> tuple[str, str]:
    """Detect a wrapper and say how to re-check the inner:
      ("string", <cmd string>) — ``<shell> -c '<str>'`` / ``eval <args>``:
          recurse on the inner command STRING (re-runs the dangerous scan
          UNQUOTED — closes the ``bash -c "rm -rf /"`` quote-evasion).
      ("segment", <remaining>)  — prefix wrapper (timeout/xargs/env/exec/nohup/…):
          re-classify the wrapper-stripped remainder (verbatim).
      ("none", segment)         — not a wrapper.
    Best-effort: an odd wrapper form lands the inner on a non-command token →
    classified "ask" (prompts) — never a wrong auto-allow, because
    _DANGEROUS_PATTERNS already ran on the full raw command.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return ("none", segment)
    if not tokens:
        return ("none", segment)
    idx = 0  # skip leading VAR=val assignments to find the command token
    while idx < len(tokens):
        t = tokens[idx]
        if "=" in t and not t.startswith("-") and t[:t.index("=")].isidentifier():
            idx += 1
            continue
        break
    if idx >= len(tokens):
        return ("none", segment)
    cmd = tokens[idx].rsplit("/", 1)[-1]
    rest = tokens[idx + 1:]

    if cmd in _EVAL_CMDS:
        # `eval <args>`: the shell concatenates the args with spaces and
        # re-parses, so the UNQUOTED join is the real command string.
        return ("string", " ".join(rest)) if rest else ("none", segment)

    if cmd in _SHELL_DASH_C:
        for k, tok in enumerate(rest):
            if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                return ("string", rest[k + 1]) if k + 1 < len(rest) else ("none", segment)
        return ("none", segment)  # interactive shell / script file — classify as-is (→ ask)

    if cmd in _PREFIX_WRAPPERS:
        j = 0
        while j < len(rest):
            t = rest[j]
            if (t.startswith("-")
                    or _DURATION_RE.match(t)
                    or t == "{}"
                    or ("=" in t and not t.startswith("-") and t[:t.index("=")].isidentifier())):
                j += 1
                continue
            break
        if j >= len(rest):
            return ("none", segment)  # bare wrapper → classify the wrapper token
        return ("segment", _drop_leading_tokens(segment, idx + 1 + j))
    return ("none", segment)


def _find_has_delete(segment: str) -> bool:
    try:
        return "-delete" in shlex.split(segment)
    except ValueError:
        return False


def _find_exec_inner(segment: str) -> str | None:
    """For ``find … -exec CMD … ;|+``, return the inner ``CMD …`` string."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    for flag in ("-exec", "-execdir", "-ok", "-okdir"):
        if flag in tokens:
            k = tokens.index(flag)
            cut: list[str] = []
            for t in tokens[k + 1:]:
                if t in (";", "+", "\\;"):
                    break
                if t == "{}":
                    continue
                cut.append(t)
            if cut:
                return " ".join(shlex.quote(t) for t in cut)
    return None


def _extract_input_redirects(segment: str) -> list[str]:
    """Extract ``< file`` input-redirect targets (read paths). Skips ``<(``
    (process substitution — handled separately) and ``<<`` (heredoc)."""
    targets: list[str] = []
    i, n = 0, len(segment)
    in_s = in_d = False
    while i < n:
        c = segment[i]
        if c == "\\" and not in_s and i + 1 < n:
            i += 2
            continue
        if c == "'" and not in_d:
            in_s = not in_s
            i += 1
            continue
        if c == '"' and not in_s:
            in_d = not in_d
            i += 1
            continue
        if in_s or in_d:
            i += 1
            continue
        if c == "<":
            nxt = segment[i + 1] if i + 1 < n else ""
            if nxt in ("(", "<", "&"):  # process subst / heredoc / fd-dup — not a file
                i += 2
                continue
            i += 1
            while i < n and segment[i] == " ":
                i += 1
            chars: list[str] = []
            while i < n and segment[i] not in (" ", ";", "&", "|", "\n", "<", ">"):
                chars.append(segment[i])
                i += 1
            if chars:
                targets.append("".join(chars).strip("'\""))
            continue
        i += 1
    return targets


def _classify_segment(
    segment: str, ctx: SecurityContext, *, is_remote: bool, depth: int,
    dangerous_only: bool = False,
) -> PathDecision:
    """Classify one pipeline segment → PathDecision(allowed, permission_tier,
    destructive). Recurses into command substitutions + wrappers (depth-capped
    by the caller).

    ``dangerous_only`` = the admin catastrophe-floor pass: still recurse through
    substitutions / wrappers / ``find -exec`` so the dangerous scan (in
    _check_command_string) reaches every nested level, but SKIP the tier /
    admin-role-gate / cross-user PATH checks — an admin-on-admin agent is
    unrestricted beyond the irreversible-catastrophe floor. Allows anything the
    floor doesn't deny."""

    def _bash_path_decision(rp: str, *, writing: bool) -> PathDecision:
        if is_remote:
            return _check_remote_bash_path(rp, ctx, writing=writing)
        resolved = _resolve_path(_translate_sandbox_path(rp, ctx))
        return _check_write_path(resolved, ctx) if writing else _check_read_path(resolved, ctx)

    # 1. Command substitutions — recurse the inner (carries the dangerous scan); a
    #    present substitution makes the OUTER command unanalyzable (the substituted
    #    value feeds the outer) → "ask" (prompt in default/acceptEdits, run dontAsk).
    inner_cmds, has_subst = _extract_substitution_inners(segment)
    sub_tier, sub_destructive = "read", False
    for inner in inner_cmds:
        if not inner.strip():
            continue
        r = _check_command_string(inner, ctx, is_remote=is_remote, depth=depth + 1,
                                  dangerous_only=dangerous_only)
        if not r.allowed:
            return r
        t = r.permission_tier or "read"
        if _TIER_ORDER.get(t, 0) > _TIER_ORDER.get(sub_tier, 0):
            sub_tier = t
        sub_destructive = sub_destructive or r.destructive
    if has_subst:
        if dangerous_only:
            return PathDecision(allowed=True, permission_tier="read")
        tier = "ask"
        if _TIER_ORDER.get(sub_tier, 0) > _TIER_ORDER.get(tier, 0):
            tier = sub_tier
        return PathDecision(allowed=True, permission_tier=tier, destructive=sub_destructive)

    # 2. Wrappers — recurse on the inner command string, or re-classify the
    #    wrapper-stripped remainder.
    kind, inner = _unwrap_segment(segment)
    if kind == "string":
        return _check_command_string(inner, ctx, is_remote=is_remote, depth=depth + 1,
                                     dangerous_only=dangerous_only)
    if kind == "segment" and inner != segment and inner.strip():
        return _classify_segment(inner, ctx, is_remote=is_remote, depth=depth + 1,
                                 dangerous_only=dangerous_only)

    # 3. Plain command.
    cmd_name = _extract_command_name(segment)
    if cmd_name is None:
        # Unparseable ≠ dangerous; the floor pass lets it through (admin is
        # unrestricted), else keep the existing hard parse-deny for non-admin.
        if dangerous_only:
            return PathDecision(allowed=True, permission_tier="read")
        return PathDecision(False, "Bash denied: could not parse command")

    # Shell control-flow keywords / no-op builtins → structural, read tier.
    if cmd_name in _SHELL_STRUCTURAL:
        return PathDecision(allowed=True, permission_tier="read")

    # find -exec <cmd> — recurse the inner (carries the dangerous scan to it, in
    # BOTH modes) so `find . -exec rm -rf / \;` is dangerous-denied; capture the
    # inner destructiveness for the non-admin tier below.
    find_exec_destructive = False
    if cmd_name == "find":
        exec_inner = _find_exec_inner(segment)
        if exec_inner:
            r = _check_command_string(exec_inner, ctx, is_remote=is_remote, depth=depth + 1,
                                      dangerous_only=dangerous_only)
            if not r.allowed:
                return r
            find_exec_destructive = r.destructive

    # Floor pass: the dangerous scan (in the caller) + the recursions above are the
    # whole check — skip tier / role-gate / path (admin unrestricted beyond it).
    if dangerous_only:
        return PathDecision(allowed=True, permission_tier="read")

    # ===== Non-admin: tier classification + admin-role-gate + path checks =====
    # Unknown command → "ask" (prompt in default/acceptEdits, run in dontAsk/auto)
    # — NEVER a hard-deny. Known commands keep their tier.
    tier = _BASH_COMMAND_TIER.get(cmd_name) or "ask"

    # Admin-tier role gating (host-touching ops — docker/systemctl/ssh/apt).
    # Local sandbox: admin only. Remote satellite: open to manager/editor/admin
    # (pairing is a trust act; the path policy is the real boundary there).
    if tier == "admin":
        if is_remote:
            if ctx.role not in ("admin", "manager", "editor"):
                return PathDecision(
                    False,
                    f"Bash denied: '{cmd_name}' is restricted to manager / "
                    f"editor / admin roles on remote satellites (current role: "
                    f"{ctx.role}).",
                )
        elif ctx.role not in ("admin",):
            return PathDecision(
                False,
                f"Bash denied: '{cmd_name}' requires platform admin role on "
                f"local sandbox sessions (current role: {ctx.role}). These "
                f"commands touch host state outside the sandbox.",
            )

    destructive = cmd_name in _DESTRUCTIVE_COMMANDS or find_exec_destructive
    if cmd_name == "find" and _find_has_delete(segment):
        destructive = True

    read_paths, write_paths = _extract_path_args(cmd_name, segment)
    for rp in read_paths:
        d = _bash_path_decision(rp, writing=False)
        if not d.allowed:
            return PathDecision(False, f"Bash denied: read path '{rp}' — {d.reason}")
    for rp in write_paths:
        d = _bash_path_decision(rp, writing=True)
        if not d.allowed:
            return PathDecision(False, f"Bash denied: write path '{rp}' — {d.reason}")
        if _TIER_ORDER.get(tier, 0) < _TIER_ORDER["edit"]:
            tier = "edit"

    for rp in _extract_redirect_targets(segment):
        if rp == "/dev/null":
            continue
        d = _bash_path_decision(rp, writing=True)
        if not d.allowed:
            return PathDecision(False, f"Bash denied: redirect target '{rp}' — {d.reason}")
        if _TIER_ORDER.get(tier, 0) < _TIER_ORDER["edit"]:
            tier = "edit"
    for rp in _extract_input_redirects(segment):
        d = _bash_path_decision(rp, writing=False)
        if not d.allowed:
            return PathDecision(False, f"Bash denied: input redirect '{rp}' — {d.reason}")

    return PathDecision(allowed=True, permission_tier=tier, destructive=destructive)


def _check_command_string(
    command: str, ctx: SecurityContext, *, is_remote: bool, depth: int,
    dangerous_only: bool = False,
) -> PathDecision:
    """Check one command string: _DANGEROUS_PATTERNS (re-run at EVERY recursion
    level) + per-segment classify. Recursed for ``bash -c``/``eval`` inners +
    command substitutions, so quote/paren-hidden dangerous content is caught.

    ``dangerous_only`` threads the admin catastrophe-floor pass through the whole
    recursion: the dangerous scan still runs at every level, but _classify_segment
    skips tier / role-gate / path (see its docstring)."""
    if depth > _MAX_BASH_DEPTH:
        return PathDecision(False, "Bash denied: command nesting too deep")
    command = command.strip()
    if not command:
        return PathDecision(allowed=True, permission_tier="read")
    # Dangerous patterns — the universal hard-deny (incl. admin-on-admin, via the
    # dangerous_only floor pass). Re-run here so an unwrapped inner (whose
    # quotes/parens defeated the outer raw scan, e.g. `bash -c "rm -rf /"`) is
    # still caught.
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return PathDecision(False, f"Bash denied: {reason}")
    try:
        segments = _split_command_segments(command)
    except ValueError as e:
        return PathDecision(False, f"Bash denied: {e}")
    if not segments:
        return PathDecision(allowed=True, permission_tier="read")
    max_tier = "read"
    saw_destructive = False
    for segment in segments:
        res = _classify_segment(segment, ctx, is_remote=is_remote, depth=depth,
                                dangerous_only=dangerous_only)
        if not res.allowed:
            return res
        t = res.permission_tier or "read"
        if _TIER_ORDER.get(t, 0) > _TIER_ORDER.get(max_tier, 0):
            max_tier = t
        saw_destructive = saw_destructive or res.destructive
    return PathDecision(allowed=True, permission_tier=max_tier, destructive=saw_destructive)


def _check_bash(command: str, ctx: SecurityContext) -> PathDecision:
    """Tiered Bash command security (exec-env v2).

    Command-type policy is defense-in-depth, NOT the cross-user boundary — bwrap
    is the boundary locally; the per-segment PATH check is the only cross-user
    boundary on shared-admin satellites (moot on local + user-paired). So:
      * unknown commands → "ask" (prompt in default/acceptEdits, run in
        dontAsk/auto) — NEVER a hard-deny (the LLM can't get past a hard-deny; a
        human can approve a prompt);
      * _DANGEROUS_PATTERNS is the UNIVERSAL hard-deny (incl. admin-on-admin via
        the dangerous_only floor pass), re-checked at every unwrap level
        (`bash -c`/`eval`/`$()` inners);
      * sub-shell wrappers + prefix wrappers + command substitutions are
        UNWRAPPED and the inner re-checked, instead of blanket-denied;
      * destructive commands (rm/dd/shred/find -delete/…) set
        PathDecision.destructive → Pass-2 prompts even in acceptEdits.

    Pipeline:
      0.  OAuth-credential-dir backstop (raw) — universal deny.
      0b. Agent-config backstop (raw) — universal deny.
      1.  Empty → deny.
      2.  Admin on admin agent → catastrophe-floor only (recursive dangerous
          scan), else unrestricted (tier/path skipped).
      3.  Non-admin → _check_command_string (recursive): dangerous + classify.
    """
    # 0/0b. Defense-in-depth backstops on the RAW command (catch literal refs
    # anywhere, incl. inside quotes/substitutions as substrings — robust to
    # wrapping). Universal, BEFORE the admin fast-path. See
    # path_roles.command_references_protected_path / _agent_config docstrings.
    from services import path_roles
    if path_roles.command_references_protected_path(command):
        return PathDecision(
            False,
            "Bash denied: command references OAuth credentials directory. "
            "Manage accounts via Settings → Integrations.",
        )
    if path_roles.command_references_protected_agent_config(command):
        return PathDecision(False, "Bash denied: agent CLI config files are protected.")

    if not command.strip():
        return PathDecision(False, "Empty bash command")

    # Remote satellites have NO bwrap → the path-policy gate IS the boundary, so
    # path args route through path_policy_v2 (home / full-FS for satellite-host,
    # per-role RBAC for in-tree). Local sessions keep agent-tree RBAC (bwrap is
    # the real boundary). Threaded into the recursion via _classify_segment.
    is_remote = ctx.target_kind in ("admin_remote", "user_remote")

    # Admin on admin agent: unrestricted for normal ops (tier / cross-user PATH
    # checks skipped) EXCEPT the irreversible-catastrophe floor — _DANGEROUS_PATTERNS
    # still apply, run recursively via the dangerous_only pass so a wrapped
    # `bash -c "rm -rf /"` / `$(rm -rf /)` is caught too. The admin-only agent is
    # the highest-value prompt-injection target and these patterns are never a
    # legitimate agent-issued action; the admin can still run them by hand on the
    # box. (The cred + agent-config backstops above already ran.)
    if ctx.is_admin_agent and ctx.role == "admin":
        floor = _check_command_string(command, ctx, is_remote=is_remote, depth=0,
                                      dangerous_only=True)
        if not floor.allowed:
            return floor
        return PathDecision(allowed=True, permission_tier="admin")

    return _check_command_string(command, ctx, is_remote=is_remote, depth=0)


# ---------------------------------------------------------------------------
# PowerShell tool — command security (cross-platform exec-env hardening)
# ---------------------------------------------------------------------------
# Claude Code ships a DISTINCT ``PowerShell`` tool on Windows (and opt-in on
# Linux/macOS via CLAUDE_CODE_USE_POWERSHELL_TOOL). Without dedicated handling it
# would hit the ``_ALLOW`` catch-all in check_tool_access → NO dangerous-deny, NO
# cross-user path check, NO credential / agent-config backstop — i.e. in
# dontAsk/auto (tasks + phone) fully-ungated PowerShell on a satellite the agent
# runs on as the operator's OS user. So PowerShell is routed through the SAME
# decision model as Bash (see _check_bash):
#   * _POWERSHELL_DANGEROUS_PATTERNS = the single hard-deny for non-admin
#     (catastrophic, never-legitimate forms only — bare-root recursive delete,
#     disk/volume format, raw drive write, HKLM nuke), re-scanned at every
#     encoded-command level;
#   * an unknown cmdlet → tier "ask" (prompt in default/acceptEdits, run in
#     dontAsk/auto) — never a hard-deny;
#   * Get-*/Select-*/… → "read" (auto-approve in default — kills the
#     prompt-on-every-command UX);
#   * destructive cmdlets (Remove-Item / Clear-Content / aliases) set
#     PathDecision.destructive → Pass-2 prompts even in acceptEdits;
#   * best-effort path extraction (-Path/-LiteralPath/positional + >/>>) routes
#     through the SAME cross-user path gate as Bash on shared-admin satellites
#     (path_policy_v2 normalizes Windows paths → the agent-config / OAuth / .ssh
#     denies apply to extracted args even when the raw-string backstops miss the
#     backslash form).
# PowerShell parsing is genuinely hard; the dangerous scan runs on the RAW string
# (robust to scriptblock/quote nesting via substring match — the safety floor),
# and anything unparseable falls to "ask" (the ASK-net).

# Approved-verb prefix → tier. Verbs are reliable intent signals for the read /
# exec / admin buckets; writers + destructives are handled by the explicit cmdlet
# sets below (which take precedence) because some verbs are ambiguous (Out-File
# writes but Out-String reads; Clear-Content truncates but Clear-Host is benign).
# Unknown verb → "ask".
_POWERSHELL_VERB_TIER: dict[str, str] = {
    # read / inspect
    "get": "read", "select": "read", "where": "read", "sort": "read",
    "measure": "read", "test": "read", "compare": "read", "find": "read",
    "search": "read", "show": "read", "format": "read", "read": "read",
    "resolve": "read", "trace": "read", "group": "read", "convertfrom": "read",
    "convertto": "read", "write": "read", "tee": "read", "split": "read",
    "join": "read", "out": "read",
    # edit / create
    "set": "edit", "new": "edit", "add": "edit", "copy": "edit", "move": "edit",
    "rename": "edit", "export": "edit", "import": "edit", "update": "edit",
    "save": "edit", "mount": "edit", "dismount": "edit", "compress": "edit",
    "expand": "edit", "edit": "edit", "checkpoint": "edit", "restore": "edit",
    # delete-ish verbs (the catastrophic forms are hard-denied; specific
    # destructive cmdlets — Remove-Item / Clear-Content — also set the
    # destructive flag below via _POWERSHELL_DESTRUCTIVE_CMDLETS).
    "remove": "edit", "clear": "edit", "reset": "edit",
    # execution / process / network / install
    "invoke": "extended", "start": "extended", "install": "extended",
    "uninstall": "extended", "register": "extended", "unregister": "extended",
    "enable": "extended", "disable": "extended", "wait": "extended",
    # host / service control (admin role on remote; admin-only on local)
    "stop": "admin", "restart": "admin", "suspend": "admin", "resume": "admin",
}

# Aliases → canonical cmdlet (lowercase). Only the ones that change the
# tier / destructiveness / path decision; unknown aliases fall to "ask".
_POWERSHELL_ALIASES: dict[str, str] = {
    "gci": "get-childitem", "ls": "get-childitem", "dir": "get-childitem",
    "gc": "get-content", "cat": "get-content", "type": "get-content",
    "gi": "get-item", "gp": "get-itemproperty", "gm": "get-member",
    "gps": "get-process", "ps": "get-process", "gsv": "get-service",
    "pwd": "get-location", "gl": "get-location", "gu": "get-unique",
    "select": "select-object", "where": "where-object", "?": "where-object",
    "foreach": "foreach-object", "%": "foreach-object", "sort": "sort-object",
    "measure": "measure-object", "group": "group-object",
    "fl": "format-list", "ft": "format-table", "fw": "format-wide",
    "echo": "write-output", "write": "write-output",
    "sc": "set-content", "set": "set-item",
    "cp": "copy-item", "copy": "copy-item", "mv": "move-item", "move": "move-item",
    "ren": "rename-item", "rni": "rename-item",
    "ni": "new-item", "md": "new-item", "mkdir": "new-item",
    "rm": "remove-item", "del": "remove-item", "rd": "remove-item",
    "ri": "remove-item", "erase": "remove-item", "rmdir": "remove-item",
    "rp": "remove-itemproperty",
    "iex": "invoke-expression", "icm": "invoke-command",
    "iwr": "invoke-webrequest", "irm": "invoke-restmethod",
    "saps": "start-process", "spps": "stop-process", "kill": "stop-process",
    "cd": "set-location", "sl": "set-location",
    "pushd": "push-location", "popd": "pop-location",
    "cls": "clear-host", "clear": "clear-host", "tee": "tee-object",
}

# Destructive cmdlets (canonical) — set PathDecision.destructive so Pass-2
# prompts EVEN in acceptEdits. These WIN over the verb tier, so Clear-Content is
# destructive while Clear-Host / Clear-Variable stay benign.
_POWERSHELL_DESTRUCTIVE_CMDLETS: set[str] = {
    "remove-item", "remove-itemproperty", "clear-content", "clear-item",
    "clear-itemproperty", "remove-partition",
}

# Explicit cmdlet → tier overrides for ambiguous-verb cmdlets (win over the verb
# map). Out-File / Out-Printer WRITE (verb "out" is otherwise read); the
# Clear-Host family + Write-Host/Output are console-only (verbs "clear"/"write").
_POWERSHELL_CMDLET_TIER: dict[str, str] = {
    "out-file": "edit", "out-printer": "edit", "tee-object": "edit",
    "clear-host": "read", "clear-variable": "read", "clear-history": "read",
    "set-location": "read", "push-location": "read", "pop-location": "read",
    "write-output": "read", "write-host": "read", "write-error": "read",
    "write-warning": "read", "write-verbose": "read", "write-debug": "read",
    "invoke-expression": "extended", "invoke-command": "extended",
    "invoke-webrequest": "extended", "invoke-restmethod": "extended",
}

# Path-bearing cmdlets → whether their path args are READ or WRITE targets (for
# the cross-user check on shared-admin). Best-effort: -Path / -LiteralPath /
# -FilePath / first positional; Copy/Move have a read SOURCE + a -Destination
# write (extracted separately). Unknown cmdlets contribute no path args (→ "ask").
_POWERSHELL_READ_PATH_CMDLETS: set[str] = {
    "get-content", "get-item", "get-childitem", "get-itemproperty",
    "test-path", "select-string", "import-csv", "import-clixml", "resolve-path",
    "copy-item", "move-item",   # SOURCE is a read (Destination handled below)
}
_POWERSHELL_WRITE_PATH_CMDLETS: set[str] = {
    "set-content", "add-content", "clear-content", "out-file", "new-item",
    "remove-item", "rename-item", "set-itemproperty", "export-csv",
    "export-clixml",
}

# Recurse / force flag fragments (PS accepts unambiguous param prefixes; -fo not
# bare -f which is ambiguous Filter-vs-Force and errors). Validated by tests.
_PS_RECURSE = r"-r(?:ecurse|ecurs|ecur|ecu|ec|e)?\b"
_PS_FORCE = r"-fo(?:rce|rc|r)?\b"
# Catastrophic, never-legitimate delete targets — BARE roots only (mirrors the
# Bash gate denying ``rm -rf /`` / ``~`` but NOT ``rm -rf ~/project``).
_PS_ROOT_TARGET = (
    r"(?:[A-Za-z]:[\\/]*|[\\/]+|~|\$home"
    r"|\$env:(?:systemroot|windir|userprofile|systemdrive|programfiles))"
    r"(?=[\"'\s;,|&]|$)"
)
_PS_DELETE_CMDLET = r"\b(?:remove-item|ri|rm|del|erase|rd|rmdir)\b"

_POWERSHELL_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Recursive force-delete of a BARE drive/posix root or home (rm -rf / analog).
    (re.compile(
        r"(?is)" + _PS_DELETE_CMDLET
        + r"(?=.*" + _PS_RECURSE + r")(?=.*" + _PS_FORCE + r")"
        + r".*?[\"'\s]" + _PS_ROOT_TARGET
    ), "Recursive force-delete of a drive / system root is blocked"),
    # Disk / volume destruction (mkfs / dd-to-device analog).
    (re.compile(r"(?i)\bformat-volume\b"), "Format-Volume is blocked"),
    (re.compile(r"(?i)\b(?:clear-disk|initialize-disk|remove-partition)\b"),
     "Disk-clearing cmdlet is blocked"),
    (re.compile(r"(?i)\\\\\.\\physicaldrive"), "Raw physical-drive write is blocked"),
    # Registry hive nuke (HKLM recursive-force delete) — bricks Windows.
    (re.compile(
        r"(?is)\b(?:remove-item|ri)\b"
        + r"(?=.*" + _PS_RECURSE + r")(?=.*" + _PS_FORCE + r")"
        + r".*\bhk(?:lm|ey_local_machine):"
    ), "Recursive force-delete of HKLM registry is blocked"),
    # CMD-syntax catastrophes (cmd /c routes here too): `rd /s <root>`,
    # `format <drive>:`. PowerShell-syntax patterns above miss these.
    (re.compile(r"(?is)\b(?:rd|rmdir)\b(?=.*/s\b).*?[\s\"']" + _PS_ROOT_TARGET),
     "Recursive directory delete of a drive root (cmd) is blocked"),
    (re.compile(r"(?i)(?:^|[\s;&|])format\s+[a-z]:"), "Drive format (cmd) is blocked"),
]

# -EncodedCommand / -enc / -e <base64-utf16le> — decode + recurse so the
# dangerous scan sees the REAL command (else base64 hides it). Validated by tests.
_PS_ENCODED_RE = re.compile(r"(?i)-e(?:c|nc|ncodedcommand)?\b\s+([A-Za-z0-9+/=]{16,})")

# Codex on Windows wraps native commands as ``powershell.exe -Command '<inner>'``
# (also ``pwsh -c`` / ``cmd /c``). The satellite bridge ROUTES these to
# _check_powershell (codex_approvals._is_windows_shell_wrapper); here we unwrap the
# COMMON form so the inner command gets full analysis (cross-user path extraction
# etc.) instead of classifying the opaque ``powershell …`` token as "ask". Only
# the value-less startup switches (-NoProfile/-NonInteractive/…) are tolerated
# before -Command; an exotic switch form falls through to "ask" (safe — the raw
# dangerous scan + encoded-command decode already ran). Validated by tests.
_PS_SHELL_UNWRAP_RE = re.compile(
    r"""(?is)^\s*(?:[a-z]:)?[\\/]?(?:[^\s'"|&;\\/]+[\\/])*"""
    r"""(?:(?:powershell|pwsh)(?:\.exe)?"""
    r"""(?:\s+-(?:noprofile|nop|nologo|noninteractive|nonint|mta|sta|windowstyle\s+\S+))*"""
    r"""\s+-c(?:ommand)?"""
    r"""|cmd(?:\.exe)?\s+/c)"""
    r"""\s+(['"]?)(.*?)\1\s*$""",
)


def _ps_unwrap_shell(command: str) -> str | None:
    """``powershell[.exe] -Command '<inner>'`` / ``pwsh -c …`` / ``cmd /c …`` →
    ``<inner>`` (one layer); None if not a recognized wrapper."""
    m = _PS_SHELL_UNWRAP_RE.match(command)
    return m.group(2) if m else None


def _ps_tokenize(s: str) -> list[str]:
    """Whitespace-split a PowerShell segment, respecting '…'/"…" quotes and
    backtick (`) escapes (PS's escape char — NOT backslash, which is a literal
    path separator). Strips surrounding quotes off each token. Raises ValueError
    on an unclosed quote."""
    tokens: list[str] = []
    cur: list[str] = []
    have = False
    i, n = 0, len(s)
    in_single = in_double = False
    while i < n:
        ch = s[i]
        if ch == "`" and not in_single and i + 1 < n:
            cur.append(s[i + 1])
            have = True
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            have = True
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            have = True
            i += 1
            continue
        if ch in (" ", "\t", "\n", "\r") and not in_single and not in_double:
            if have:
                tokens.append("".join(cur))
                cur = []
                have = False
            i += 1
            continue
        cur.append(ch)
        have = True
        i += 1
    if in_single or in_double:
        raise ValueError("Unclosed quote in PowerShell command")
    if have:
        tokens.append("".join(cur))
    return tokens


def _split_powershell_segments(command: str) -> list[str]:
    """Split a PowerShell command into pipeline / statement segments on unquoted
    ``;`` ``|`` ``||`` ``&&`` and newlines. PowerShell quoting differs from POSIX: backtick is
    the escape / line-continuation char (NOT backslash) and '…'/"…" delimit
    strings. Conservative: brackets are NOT tracked, so a separator inside
    ``$(...)`` / ``{...}`` over-splits — which only classifies the inner pieces
    separately (safe; the raw dangerous scan already ran on the whole string). A
    lone ``&`` is NOT a separator (so ``2>&1`` stays intact). Raises ValueError on
    an unclosed quote."""
    segments: list[str] = []
    current: list[str] = []
    i, n = 0, len(command)
    in_single = in_double = False
    while i < n:
        ch = command[i]
        if ch == "`" and not in_single and i + 1 < n:
            current.append(ch)
            current.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if in_single or in_double:
            current.append(ch)
            i += 1
            continue
        # A newline / carriage-return is a PowerShell statement separator (a
        # backtick-newline continuation is consumed above), so later lines
        # can't inherit the first line's classification.
        if ch == "\n" or ch == "\r":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 1
            continue
        if ch == ";":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 1
            continue
        if ch == "|":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 2 if (i + 1 < n and command[i + 1] == "|") else 1
            continue
        if ch == "&" and i + 1 < n and command[i + 1] == "&":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 2
            continue
        current.append(ch)
        i += 1
    if in_single or in_double:
        raise ValueError("Unclosed quote in PowerShell command")
    seg = "".join(current).strip()
    if seg:
        segments.append(seg)
    return segments


def _ps_normalize_token(token: str) -> str:
    """Lowercase + strip path prefix / .exe / quotes + resolve alias → canonical
    cmdlet name for one PowerShell token."""
    t = token.strip("'\"")
    t = t.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if t.lower().endswith(".exe"):
        t = t[:-4]
    low = t.lower()
    return _POWERSHELL_ALIASES.get(low, low)


def _ps_cmdlet_name(segment: str) -> str | None:
    """Canonical cmdlet name for a PS segment (alias-resolved, lowercase). Skips a
    leading call/dot-source operator (``&`` / ``.``) and a ``$var =`` assignment so
    the RHS command is classified. None for an empty segment."""
    seg = segment.strip()
    if not seg:
        return None
    if seg[:1] in ("&", ".") and (len(seg) == 1 or seg[1:2] in (" ", "\t", "\"", "'")):
        seg = seg[1:].strip()
    m = re.match(r"^\$[A-Za-z_][\w:]*\s*=\s*(.*)$", seg, re.DOTALL)
    if m:
        seg = m.group(1).strip()
    if not seg:
        return None
    try:
        first = _ps_tokenize(seg)[0]
    except (ValueError, IndexError):
        return None
    return _ps_normalize_token(first)


def _ps_is_literal_path(tok: str) -> bool:
    """True if ``tok`` is a static path we can cross-user-check (not a variable /
    subexpression / splat we cannot resolve)."""
    tok = tok.strip("'\"")
    if not tok or tok.startswith("-"):
        return False
    if "$" in tok or tok[:1] in ("(", "@", "{"):
        return False
    return True


_PS_PATH_FLAGS = frozenset({"-path", "-literalpath", "-filepath"})
# Value-LESS switch params: a following bareword is a positional path, NOT the
# flag's value. A flag NOT listed here is assumed value-taking → its next token
# is skipped for positional extraction (so e.g. ``-Value hi`` doesn't check ``hi``
# as a bogus out-of-scope path → false-deny). Erring toward "value-taking" only
# MISSES a cross-user check (safe residual); wrongly listing a value-flag here
# would false-deny its value.
_PS_SWITCH_FLAGS = frozenset({
    "-recurse", "-r", "-force", "-fo", "-whatif", "-confirm", "-verbose",
    "-debug", "-passthru", "-nonewline", "-append", "-wait", "-quiet",
    "-asjob", "-hidden", "-directory", "-file", "-readonly", "-followsymlink",
})


def _extract_powershell_paths(cmdlet: str, segment: str) -> tuple[list[str], list[str]]:
    """Best-effort literal path args for the cross-user gate → (reads, writes).
    Pulls -Path / -LiteralPath / -FilePath values, -Destination (always write),
    the first positional path (for path cmdlets) and ``>``/``>>`` targets. Skips
    variable / subexpression args (documented residual, same as Bash)."""
    reads: list[str] = []
    writes: list[str] = []
    writing = cmdlet in _POWERSHELL_WRITE_PATH_CMDLETS
    path_cmdlet = writing or cmdlet in _POWERSHELL_READ_PATH_CMDLETS
    try:
        tokens = _ps_tokenize(segment)
    except ValueError:
        tokens = []
    # Start after the cmdlet token (first token whose normalized form == cmdlet).
    start = 1
    for idx, raw in enumerate(tokens):
        if _ps_normalize_token(raw) == cmdlet:
            start = idx + 1
            break
    i = start
    positional_taken = False
    prev_was_value_flag = False  # previous token was a flag that likely TAKES a value
    while i < len(tokens):
        raw = tokens[i]
        low = raw.lower()
        if low in _PS_PATH_FLAGS:
            if i + 1 < len(tokens) and _ps_is_literal_path(tokens[i + 1]):
                (writes if writing else reads).append(tokens[i + 1])
            i += 2
            prev_was_value_flag = False
            continue
        if low == "-destination":
            if i + 1 < len(tokens) and _ps_is_literal_path(tokens[i + 1]):
                writes.append(tokens[i + 1])
            i += 2
            prev_was_value_flag = False
            continue
        if raw.startswith("-"):
            # A switch (-Recurse/-Force/…) leaves the NEXT bareword a positional
            # path; a value-taking flag (-Value/-Encoding/-Filter/…) consumes it.
            prev_was_value_flag = low not in _PS_SWITCH_FLAGS
            i += 1
            continue
        if (not prev_was_value_flag and path_cmdlet and not positional_taken
                and _ps_is_literal_path(raw)):
            positional_taken = True
            (writes if writing else reads).append(raw)
        prev_was_value_flag = False
        i += 1
    for rp in _extract_redirect_targets(segment):
        if rp != "/dev/null":
            writes.append(rp)
    return reads, writes


def _classify_powershell_segment(
    segment: str, ctx: SecurityContext, *, is_remote: bool, depth: int,
    dangerous_only: bool = False,
) -> PathDecision:
    """Classify one PowerShell segment → PathDecision(allowed, tier, destructive).

    ``dangerous_only`` = the admin catastrophe-floor pass. The PS dangerous scan +
    the -EncodedCommand / shell-wrapper recursion all live in the caller
    (_check_powershell_string), so a segment carries no further dangerous content
    to recurse into — the floor pass simply returns allow (skipping tier /
    role-gate / cross-user PATH; admin is unrestricted beyond the floor)."""

    def _ps_path_decision(rp: str, *, writing: bool) -> PathDecision:
        if is_remote:
            return _check_remote_bash_path(rp, ctx, writing=writing)
        resolved = _resolve_path(_translate_sandbox_path(rp, ctx))
        return _check_write_path(resolved, ctx) if writing else _check_read_path(resolved, ctx)

    cmdlet = _ps_cmdlet_name(segment)
    if cmdlet is None:
        return PathDecision(allowed=True, permission_tier="read")

    if dangerous_only:
        return PathDecision(allowed=True, permission_tier="read")

    destructive = cmdlet in _POWERSHELL_DESTRUCTIVE_CMDLETS
    tier = _POWERSHELL_CMDLET_TIER.get(cmdlet)
    if tier is None:
        verb = cmdlet.split("-", 1)[0] if "-" in cmdlet else ""
        tier = _POWERSHELL_VERB_TIER.get(verb, "ask")
    if destructive and _TIER_ORDER.get(tier, 0) < _TIER_ORDER["edit"]:
        tier = "edit"

    # Admin-tier role gating (host / service control) — mirror _classify_segment.
    if tier == "admin":
        if is_remote:
            if ctx.role not in ("admin", "manager", "editor"):
                return PathDecision(
                    False,
                    f"PowerShell denied: '{cmdlet}' is restricted to manager / "
                    f"editor / admin roles on remote satellites (current role: "
                    f"{ctx.role}).",
                )
        elif ctx.role not in ("admin",):
            return PathDecision(
                False,
                f"PowerShell denied: '{cmdlet}' requires platform admin role "
                f"(current role: {ctx.role}).",
            )

    reads, writes = _extract_powershell_paths(cmdlet, segment)
    for rp in reads:
        d = _ps_path_decision(rp, writing=False)
        if not d.allowed:
            return PathDecision(False, f"PowerShell denied: read path '{rp}' — {d.reason}")
    for rp in writes:
        d = _ps_path_decision(rp, writing=True)
        if not d.allowed:
            return PathDecision(False, f"PowerShell denied: write path '{rp}' — {d.reason}")
        if _TIER_ORDER.get(tier, 0) < _TIER_ORDER["edit"]:
            tier = "edit"

    return PathDecision(allowed=True, permission_tier=tier, destructive=destructive)


def _check_powershell_string(
    command: str, ctx: SecurityContext, *, is_remote: bool, depth: int,
    dangerous_only: bool = False,
) -> PathDecision:
    """Check one PowerShell command string: _POWERSHELL_DANGEROUS_PATTERNS (raw,
    re-run at every level) + -EncodedCommand decode/recurse + per-segment
    classify.

    ``dangerous_only`` threads the admin catastrophe-floor pass through the
    recursion: the dangerous scan + encoded/wrapper unwrap still run at every
    level, but _classify_powershell_segment skips tier / role-gate / path."""
    if depth > _MAX_BASH_DEPTH:
        return PathDecision(False, "PowerShell denied: command nesting too deep")
    command = command.strip()
    if not command:
        return PathDecision(allowed=True, permission_tier="read")
    for pattern, reason in _POWERSHELL_DANGEROUS_PATTERNS:
        if pattern.search(command):
            return PathDecision(False, f"PowerShell denied: {reason}")
    # -EncodedCommand <base64-utf16le> → decode + recurse so the dangerous scan
    # sees the real command. An undecodable blob → "ask" (prompts in
    # default/acceptEdits; the raw scan above already ran on the outer string).
    enc_tier, enc_destr, saw_enc = "read", False, False
    for m in _PS_ENCODED_RE.finditer(command):
        saw_enc = True
        try:
            inner = base64.b64decode(m.group(1)).decode("utf-16-le")
        except Exception:
            inner = ""
        if not inner.strip():
            continue
        r = _check_powershell_string(inner, ctx, is_remote=is_remote, depth=depth + 1,
                                     dangerous_only=dangerous_only)
        if not r.allowed:
            return r
        t = r.permission_tier or "read"
        if _TIER_ORDER.get(t, 0) > _TIER_ORDER.get(enc_tier, 0):
            enc_tier = t
        enc_destr = enc_destr or r.destructive
    if saw_enc:
        if dangerous_only:
            return PathDecision(allowed=True, permission_tier="read")
        tier = "ask"
        if _TIER_ORDER.get(enc_tier, 0) > _TIER_ORDER.get(tier, 0):
            tier = enc_tier
        return PathDecision(allowed=True, permission_tier=tier, destructive=enc_destr)
    # Shell-wrapper unwrap (Codex's `powershell.exe -Command '<inner>'`, `cmd /c`)
    # → recurse on the inner so it gets full classification + cross-user paths.
    unwrapped = _ps_unwrap_shell(command)
    if unwrapped is not None and unwrapped.strip() and unwrapped != command:
        return _check_powershell_string(unwrapped, ctx, is_remote=is_remote, depth=depth + 1,
                                        dangerous_only=dangerous_only)
    try:
        segments = _split_powershell_segments(command)
    except ValueError as e:
        return PathDecision(False, f"PowerShell denied: {e}")
    if not segments:
        return PathDecision(allowed=True, permission_tier="read")
    max_tier, saw_destructive = "read", False
    for segment in segments:
        res = _classify_powershell_segment(segment, ctx, is_remote=is_remote, depth=depth,
                                           dangerous_only=dangerous_only)
        if not res.allowed:
            return res
        t = res.permission_tier or "read"
        if _TIER_ORDER.get(t, 0) > _TIER_ORDER.get(max_tier, 0):
            max_tier = t
        saw_destructive = saw_destructive or res.destructive
    return PathDecision(allowed=True, permission_tier=max_tier, destructive=saw_destructive)


def _check_powershell(command: str, ctx: SecurityContext) -> PathDecision:
    """Tiered PowerShell command security — the Windows-shell sibling of
    _check_bash, same defense-in-depth model (command policy is NOT the cross-user
    boundary; the per-segment PATH check is, on shared-admin satellites).

    Pipeline (mirrors _check_bash exactly):
      0.  OAuth-credential-dir backstop (raw) — universal deny.
      0b. Agent-config backstop (raw) — universal deny.
      1.  Empty → deny.
      2.  Admin on admin agent → catastrophe-floor only (recursive dangerous
          scan), else unrestricted (tier/path skipped).
      3.  Non-admin → _check_powershell_string (recursive): dangerous + encoded +
          classify.
    """
    from services import path_roles
    if path_roles.command_references_protected_path(command):
        return PathDecision(
            False,
            "PowerShell denied: command references OAuth credentials directory. "
            "Manage accounts via Settings → Integrations.",
        )
    if path_roles.command_references_protected_agent_config(command):
        return PathDecision(False, "PowerShell denied: agent CLI config files are protected.")

    if not command.strip():
        return PathDecision(False, "Empty PowerShell command")

    is_remote = ctx.target_kind in ("admin_remote", "user_remote")

    # Admin on admin agent: unrestricted for normal ops EXCEPT the
    # irreversible-catastrophe floor (Format-Volume, Clear-Disk, bare-root
    # recursive delete, …) — _POWERSHELL_DANGEROUS_PATTERNS still apply, run
    # recursively via the dangerous_only pass (so an encoded / `cmd /c`-wrapped
    # catastrophe is caught too). Mirrors _check_bash; admin can still run these by
    # hand on the box. (Cred + agent-config backstops above already ran.)
    if ctx.is_admin_agent and ctx.role == "admin":
        floor = _check_powershell_string(command, ctx, is_remote=is_remote, depth=0,
                                         dangerous_only=True)
        if not floor.allowed:
            return floor
        return PathDecision(allowed=True, permission_tier="admin")

    return _check_powershell_string(command, ctx, is_remote=is_remote, depth=0)


# ---------------------------------------------------------------------------
# WebFetch — SSRF prevention
# ---------------------------------------------------------------------------


def _check_webfetch(url: str, ctx: SecurityContext) -> PathDecision:
    """Block WebFetch to private/internal IPs for non-admin agents."""
    if ctx.is_admin_agent and ctx.role == "admin":
        return _ALLOW

    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
    except Exception:
        return PathDecision(False, "WebFetch denied: malformed URL")

    if not hostname:
        return PathDecision(False, "WebFetch denied: no hostname in URL")

    # Check literal hostnames
    if hostname.lower() in _PRIVATE_HOSTNAMES:
        return PathDecision(False, "WebFetch denied: private/internal address")
    for suffix in _PRIVATE_SUFFIXES:
        if hostname.lower().endswith(suffix):
            return PathDecision(False, "WebFetch denied: private/internal address")

    # Check if hostname is an IP address in private ranges
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return PathDecision(False, "WebFetch denied: private/internal address")
    except ValueError:
        pass  # Not an IP literal — allow (Anthropic's server-side has its own protections)

    return _ALLOW
