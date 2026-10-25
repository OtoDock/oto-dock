#!/usr/bin/env python3
"""Regenerate proxy/auth/gh_flags.py from gh's own help.

The judge of a gh command (``proxy/auth/github_reach.judge_gh``) reads a
command the way gh does, so it needs each command's flags and whether
they take a value. This script asks the installed ``gh`` (``GH`` in the
environment, else the one on PATH; inside an OtoDock session the PATH's
``gh`` is the platform's wrapper, so pass ``GH=/usr/bin/gh``) for every
command's ``--help`` and writes the table. Run it after a gh release, from the repo root, and
commit the result with the gh version it names::

    scripts/gen-gh-flags.py

A flag with a word after its name (``-s, --state string``) takes a value;
one without does not. A third level (``gh repo deploy-key list``) folds
into its verb. A flag whose arity differs between a group's commands is
listed by command.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

GROUPS = ["repo", "pr", "issue", "release", "run", "workflow", "label", "gist", "cache", "ruleset",
          "auth", "search", "status", "browse", "api", "secret", "variable"]
FLAG = re.compile(r"^\s+(?:(-[A-Za-z0-9]), )?(--[A-Za-z0-9][\w-]*)( \S+)?\s{2,}\S")
FLAG_END = re.compile(r"^\s+(?:(-[A-Za-z0-9]), )?(--[A-Za-z0-9][\w-]*)( \S+)?\s*$")
GLOBAL_BOOL = {"--help", "-h"}
TARGET = Path(__file__).resolve().parent.parent / "proxy" / "auth" / "gh_flags.py"


def gh_env(home: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GH_NO_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1", "HOME": home}


def run_gh(gh: str, home: str, *words: str) -> str:
    r = subprocess.run([gh, *words], capture_output=True, text=True, env=gh_env(home))
    return r.stdout + r.stderr


def verbs_of(text: str) -> list[str]:
    out: list[str] = []
    on = False
    for line in text.splitlines():
        if re.match(r"^[A-Z ]*COMMANDS", line):
            on = True
            continue
        if on:
            if not line.strip():
                if out:
                    on = False
                continue
            m = re.match(r"^\s+([a-z][\w-]*):?\s", line)
            if m:
                out.append(m.group(1))
            elif not line.startswith(" "):
                on = False
    return out


def flags_of(text: str) -> tuple[set[str], set[str]]:
    value: set[str] = set()
    boolean: set[str] = set()
    for line in text.splitlines():
        m = FLAG.match(line) or FLAG_END.match(line)
        if not m:
            continue
        short, long_, arg = m.groups()
        target = value if arg else boolean
        target.add(long_)
        if short:
            target.add(short)
    return value, boolean


def read_table(gh: str, home: str) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for group in GROUPS:
        text = run_gh(gh, home, group, "--help")
        verbs = verbs_of(text)
        v, b = flags_of(text)
        table[group] = {"value": sorted(v), "bool": sorted(b), "verbs": verbs}
        for verb in verbs:
            sub = run_gh(gh, home, group, verb, "--help")
            v, b = flags_of(sub)
            table[f"{group} {verb}"] = {"value": sorted(v), "bool": sorted(b)}
            for sv in verbs_of(sub):
                v2, b2 = flags_of(run_gh(gh, home, group, verb, sv, "--help"))
                table[f"{group} {verb} {sv}"] = {"value": sorted(v2), "bool": sorted(b2)}
    return table


def fmt(name: str, d: dict[str, set[str]]) -> str:
    out = [f"{name}: dict[str, frozenset[str]] = {{"]
    for k in sorted(d):
        flags = sorted(d[k])
        if not flags:
            continue
        line = f'    "{k}": frozenset({{'
        body = ", ".join(f'"{f}"' for f in flags)
        text = line + body + "}),"
        if len(text) <= 110:
            out.append(text)
        else:
            out.append(line)
            cur = "        "
            for f in flags:
                piece = f'"{f}", '
                if len(cur) + len(piece) > 110:
                    out.append(cur.rstrip())
                    cur = "        "
                cur += piece
            out.append(cur.rstrip())
            out.append("    }),")
    out.append("}")
    return "\n".join(out)


def render(table: dict[str, dict], version: str) -> str:
    groups: dict[str, list] = {}
    for k, v in table.items():
        words = k.split()
        cmd = " ".join(words[:2])
        groups.setdefault(words[0], []).append((cmd, set(v["value"]), set(v["bool"])))
    value: dict[str, set[str]] = {}
    boolean: dict[str, set[str]] = {}
    cv: dict[str, set[str]] = {}
    cb: dict[str, set[str]] = {}
    for g, rows in sorted(groups.items()):
        gv = set().union(*[r[1] for r in rows])
        gb = set().union(*[r[2] for r in rows])
        conflicts = gv & gb
        value[g] = gv - conflicts
        boolean[g] = gb - conflicts - GLOBAL_BOOL
        for cmd, v, b in rows:
            if v & conflicts:
                cv.setdefault(cmd, set()).update(v & conflicts)
            if b & conflicts:
                cb.setdefault(cmd, set()).update(b & conflicts)
    for cmd in set(cv) & set(cb):
        if cv[cmd] & cb[cmd]:
            raise SystemExit(f"a flag of 'gh {cmd}' is both kinds: {sorted(cv[cmd] & cb[cmd])}")
    src = f'''"""gh's flags by command, read from gh's own help (gh {version}): the ones
that take a value and the ones that do not. The judge of a gh command
(``auth/github_reach.judge_gh``) skips a value-taking flag's value, so a
value is never read as the command's argument, and refuses a flag it does
not know when a word follows it under a narrowed reach (the word could be
either). A flag whose arity differs between a group's commands is listed by
command; a third level (``gh repo deploy-key list``) folds into its verb.

Generated by ``scripts/gen-gh-flags.py``: run it after a gh release.
"""

from __future__ import annotations

# Flags every command takes, none with a value.
GLOBAL_BOOLEAN = frozenset({{"--help", "-h"}})

'''
    src += fmt("VALUE", value) + "\n\n" + fmt("BOOLEAN", boolean) + "\n\n"
    src += "# The flags whose arity differs between a group's commands, by command.\n"
    src += fmt("COMMAND_VALUE", cv) + "\n\n" + fmt("COMMAND_BOOLEAN", cb) + "\n"
    src += '''

def takes_value(group: str, verb: str, flag: str) -> bool | None:
    """Whether ``flag`` of ``gh <group> <verb>`` takes a value: ``None``
    for a flag gh's help does not list for it."""
    command = f"{group} {verb}" if verb else group
    if flag in COMMAND_VALUE.get(command, ()):
        return True
    if flag in COMMAND_BOOLEAN.get(command, ()):
        return False
    if flag in VALUE.get(group, ()):
        return True
    if flag in BOOLEAN.get(group, ()) or flag in GLOBAL_BOOLEAN:
        return False
    return None
'''
    return src


def main() -> int:
    gh = os.environ.get("GH", "gh")
    with tempfile.TemporaryDirectory(prefix="gh-flags-") as home:
        first = run_gh(gh, home, "--version").splitlines()[0] if run_gh(gh, home, "--version") else ""
        m = re.search(r"gh version (\S+)", first)
        if not m:
            print(f"gh did not answer --version ({gh}): {first!r}", file=sys.stderr)
            return 1
        version = m.group(1)
        table = read_table(gh, home)
    if "--json" in sys.argv:
        print(json.dumps(table, indent=1))
        return 0
    TARGET.write_text(render(table, version))
    print(f"{TARGET.relative_to(TARGET.parents[2])}: gh {version}, {len(table)} commands read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
