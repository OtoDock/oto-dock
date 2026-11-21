"""A session's tool processes, read from inside a container, and the one call the rig
makes to start a tool: the memory tool every agent has, which needs no account."""
from __future__ import annotations

import subprocess

# Where a paired machine keeps its satellite and its tools, under the account's home.
MACHINE_FOLDER = "/.oto-dock/"
MEMORY_TOOL = "mcp__memory-mcp__memory"
# By the last part of its name, which every engine keeps: Codex's names carry the
# server key with its dashes as underscores.
READ_MEMORY = {"tool": "memory", "input": {"command": "view", "path": "/memories/agent/"}}

# The one read of the processes: each one whose command line names the shell's first
# argument, as its OOM score and its command line, from /proc (the images have no ps).
# The read's own shell names /proc and is left out.
PROCS = ("for p in /proc/[0-9]*; do l=$(tr '\\0' ' ' < $p/cmdline 2>/dev/null); "
         "case \"$l\" in *\"$0\"*) case \"$l\" in *'/proc/'*) ;; *) echo \"$(cat $p/oom_score_adj 2>/dev/null) $l\";; esac;; esac; done")
# What a tool's stand-in runs: the interceptor, with the program's own command after
# its ``--`` (``core/stdio_path_interceptor.py``). Its program, once started, is a
# child of its own whose first argument is the tool's own interpreter, under the
# tool's folder; a sandbox's launchers name that folder only further on.
STAND_IN = "stdio_path_interceptor"


def parse_scores(out: str) -> list[tuple[int, str]]:
    """``PROCS``'s lines as (score, command line)."""
    found = []
    for line in out.splitlines():
        score, _, command = line.strip().partition(" ")
        if score.lstrip("-").isdigit() and command:
            found.append((int(score), command.strip()))
    return found


def tool_scores(ctx, service: str, word: str) -> list[tuple[int, str]]:
    """(score, command line) of each process in a service's container that names ``word``."""
    done = ctx.compose("exec", "-T", service, "sh", "-c", PROCS, word, timeout=60)
    return parse_scores(done.stdout)


def host_scores(ctx, word: str) -> list[tuple[int, str]]:
    """The same on this host, where the paired machine's sessions are this account's
    processes. The containers' processes show here too: a caller keeps the machine's
    own by their folder (``MACHINE_FOLDER``)."""
    ctx.require_rig()
    done = subprocess.run(["sh", "-c", PROCS, word], capture_output=True, text=True, timeout=60, check=False)
    return parse_scores(done.stdout)


def tool_procs(ctx, service: str, tool: str) -> list[str]:
    """The command lines naming ``tool`` in a service's container: its stand-ins and
    its programs alike."""
    return [command for _, command in tool_scores(ctx, service, tool)]


def runs(word: str, command: str) -> bool:
    """The process is the program itself, or an interpreter running it: its first or
    second argument is ``word`` (a path, a ``.js`` script). A process that only names
    it further on is not."""
    names = [part.rsplit("/", 1)[-1] for part in command.split()[:2]]
    return word in names or f"{word}.js" in names


def programs_of(lines: list[str], tool: str) -> list[str]:
    """The lines that are the tool's own program: not its stand-in, and not the
    sandbox's launchers (pasta, bwrap) that carry its command."""
    return [line for line in lines if f"/{tool}/" in line.split(" ", 1)[0]]


def stand_ins_of(lines: list[str]) -> list[str]:
    return [line for line in lines if STAND_IN in line]
