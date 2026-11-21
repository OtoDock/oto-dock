#!/bin/sh
# Copy the community tools the server holds into the compute machine's own
# tools folder, once.
#
#   copy-tools-to-compute.sh <the server's tools folder> <the compute machine's tools folder>
#
# The compute machine builds and keeps the community tools in a volume of its
# own. An install that updates from a release where the server built them has
# them, built, in the server's volume: the compose file's otodock-tools-copy
# one-shot runs this before either service starts, so no tool is built again
# at that update. It runs as the compute machine's own account (uid 1000) and
# sees only the two folders, the server's read-only: the compute machine
# writes the destination, so the copy writes nothing it could not write
# itself, and takes from the server's folder only the programs it runs.
#
# - Copied: every program's folder (one whose manifest.json names a Python
#   or a Node runtime), with or without its environment, and every
#   interpreter of .uv-python, with owner, mode and links kept.
# - Left out: a Docker tool's folder (its compose file, the .env with the
#   secrets an admin set: the server runs it, and the compute machine holds
#   no secret of the platform), a tool with nothing to run, an update's
#   backup (*.bak), a failed install put aside (*.failed), a staged folder
#   (*.new) and every hidden entry but .uv-python.
# - A tool the destination already holds is left as it is: the compute
#   machine's copy is the live one.
# - Each folder is copied under a staging name and renamed, so the
#   destination never holds half a tool. A run that stopped leaves no mark,
#   and the next start copies what is still missing.
# - The copy is the one hand-over. The mark (.tools-copied) is written last
#   in the compute machine's folder, and with it the script does nothing.
#   A compute volume that was removed is filled again from what the server
#   holds. The server keeps no environment of a tool the compute machine
#   built, so such a tool arrives without one and the compute machine
#   installs it again.
# - Too little free space: one line, exit 3, no mark. The compute machine
#   then installs each tool again by itself.
#
# Exit 0 copied or nothing to do, 1 a folder could not be copied, 2 usage or
# a missing folder, 3 too little free space.
set -u

MARK=".tools-copied"
STAGING=".copying-"

say() { printf 'copy-tools-to-compute: %s\n' "$*"; }

if [ "$#" -ne 2 ]; then
    say "usage: copy-tools-to-compute.sh <the server's tools folder> <the compute machine's tools folder>" >&2
    exit 2
fi
src=$1
dst=$2
if [ ! -d "$src" ] || [ ! -d "$dst" ]; then
    say "a tools folder is missing: $src or $dst" >&2
    exit 2
fi

mark() { # mark <folder>: written whole or not at all, through a new file of its own
    tmp=$(mktemp "$1/$MARK.XXXXXX") || return 1
    if printf 'copied %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" && mv -fT "$tmp" "$1/$MARK"; then
        return 0
    fi
    rm -f "$tmp"
    return 1
}

[ -e "$dst/$MARK" ] && exit 0

# What a run that stopped left under a staging name.
for left in "$dst/$STAGING"* "$dst/.uv-python/$STAGING"*; do
    if [ -e "$left" ] || [ -L "$left" ]; then
        rm -rf "$left"
    fi
done

held() { [ -e "$1" ] || [ -L "$1" ]; }

need=0
measure() { # measure <from>
    kib=$(du -sk "$1" 2>/dev/null | cut -f1)
    case "$kib" in ''|*[!0-9]*) kib=0 ;; esac
    need=$((need + kib))
}

copied=0
failed=""
copy() { # copy <from> <to> <kind>
    stage="${2%/*}/$STAGING${2##*/}"
    [ -d "${2%/*}" ] || mkdir -p "${2%/*}"
    if cp -pPR "$1" "$stage" && mv -T "$stage" "$2"; then
        [ "$3" = tool ] && copied=$((copied + 1))
        return 0
    fi
    rm -rf "$stage"
    failed="$failed ${1##*/}"
}

handle() { # handle measure|copy <from> <to> <kind>
    case "$1" in
        measure) measure "$2" ;;
        copy) copy "$2" "$3" "$4" ;;
    esac
}

each() { # each measure|copy: every folder to copy, as <from> <to> <kind>
    for from in "$src"/*; do
        name=${from##*/}
        case "$name" in *.bak|*.failed|*.new) continue ;; esac
        if [ -L "$from" ] || [ ! -d "$from" ] || [ ! -f "$from/manifest.json" ]; then
            continue
        fi
        # Only what the compute machine runs: a Python or Node program.
        grep -Eiq '"runtime"[[:space:]]*:[[:space:]]*"[[:space:]]*(python|node)[[:space:]]*"' "$from/manifest.json" \
            || continue
        [ -f "$from/docker-compose.yml" ] && continue
        held "$dst/$name" && continue
        handle "$1" "$from" "$dst/$name" tool
    done
    for from in "$src/.uv-python"/*; do
        held "$from" || continue
        held "$dst/.uv-python/${from##*/}" && continue
        handle "$1" "$from" "$dst/.uv-python/${from##*/}" interpreter
    done
}

each measure

# OTODOCK_TOOLS_COPY_TEST_FREE_KIB is the tests' stand-in for the disk.
free=${OTODOCK_TOOLS_COPY_TEST_FREE_KIB:-$(df -Pk "$dst" 2>/dev/null | awk 'NR == 2 { print $4 }')}
case "$free" in ''|*[!0-9]*) free="" ;; esac
# A twentieth on top: the copy lands on a volume that also takes new builds.
if [ -n "$free" ] && [ "$free" -lt $((need + need / 20)) ]; then
    say "The community tools were not copied to the compute machine. They need $((need / 1024 + 1)) MB and its tools volume has $((free / 1024)) MB free. OtoDock installs each tool again there, which takes a while. To copy them instead, free disk space and start OtoDock again."
    exit 3
fi

each copy

if [ -n "$failed" ]; then
    say "These could not be copied to the compute machine:$failed. OtoDock installs them again there, or the next start copies them, whichever comes first."
    exit 1
fi
mark "$dst" || exit 1
if [ "$copied" -eq 1 ]; then
    say "Copied 1 community tool to the compute machine."
elif [ "$copied" -gt 1 ]; then
    say "Copied $copied community tools to the compute machine."
fi
exit 0
