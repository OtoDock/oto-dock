#!/usr/bin/env bash
#
# OtoDock runtime version source — exports the pinned image/version variables
# parsed straight out of VERSIONS.md (the single source of truth) so the Docker
# build, Compose, and any wrapper all read ONE file. Bumping a version in
# VERSIONS.md propagates to the image build with no Dockerfile edit.
#
# Sourceable:
#     source scripts/versions.sh        # exports the keys into the environment
#     echo "$PYTHON_IMAGE"              # -> python:3.13.14-slim-bookworm
#
# Or query a single key:
#     scripts/versions.sh PYTHON_IMAGE   # prints the value (or nothing)
#
# Grammar mirrors proxy/config.py::_read_pinned_version exactly: a line of the
# form `KEY=value` (the `=` lines inside VERSIONS.md's code fences). Keys absent
# from VERSIONS.md are left unset, so the Dockerfile `ARG` / Compose `${VAR:-…}`
# literal default takes over — a missing/garbled VERSIONS.md is never fatal.

_otodock_versions_md="${OTODOCK_VERSIONS_MD:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/VERSIONS.md}"

# otodock_version_get KEY — echo the value of `KEY=value` from VERSIONS.md, or
# nothing if the key is absent / the file is unreadable.
otodock_version_get() {
    local key="$1"
    [ -r "$_otodock_versions_md" ] || return 0
    sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p" \
        "$_otodock_versions_md" | head -1
}

# Direct invocation (not sourced): print one key's value and exit.
if [ "${BASH_SOURCE[0]:-}" = "${0:-}" ] && [ "$#" -ge 1 ]; then
    otodock_version_get "$1"
    exit 0
fi

# Sourced: export the build-relevant keys (only when found, so the build keeps
# its baked defaults if a key is missing). PYTHON_IMAGE / NODE_IMAGE feed the
# Compose `build.args`; POSTGRES_IMAGE / OTODOCK_VERSION pin the runtime images.
for _otodock_key in PYTHON_IMAGE NODE_IMAGE POSTGRES_IMAGE OTODOCK_VERSION; do
    _otodock_val="$(otodock_version_get "$_otodock_key")"
    [ -n "$_otodock_val" ] && export "${_otodock_key}=${_otodock_val}"
done
unset _otodock_key _otodock_val
