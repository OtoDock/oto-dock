#!/usr/bin/env bash
#
# Which model a paired machine's Claude Code talks to on this host. A machine has
# no sandbox, so Claude Code's own managed settings reach it, and they apply to
# every session on the host: `scripted` while the scripted layer runs, `vendor`
# before the live layer. The proxy's sessions decide by their key (scripted-engine/claude).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rig/install/guard.sh
. "$HERE/guard.sh"
FILE=/etc/claude-code/managed-settings.json
case "${1:-}" in
    scripted)
        sudo mkdir -p /etc/claude-code
        echo '{"env":{"ANTHROPIC_BASE_URL":"http://198.18.0.1:18099","CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1"}}' \
            | sudo tee "$FILE" >/dev/null ;;
    vendor) sudo rm -f "$FILE" ;;
    *) echo "usage: engines.sh scripted|vendor" >&2; exit 2 ;;
esac
