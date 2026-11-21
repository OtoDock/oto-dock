# shellcheck shell=bash
#
# Sourced first by every script of this folder. They act on the machine they run
# on (the container engine, the service manager, /etc, the home folder), so each
# proves it is on a rig before any command that changes anything, and a refusal
# says "Nothing was touched."
#
# A rig is a host whose own home holds a marker naming this host's name and
# machine id. Nothing here writes it: the command that drives the rig writes it,
# after checking that host. The home is the password database's. A service manager
# and a container engine do not read HOME, so a fake HOME isolates nothing, and it
# is refused.
#
# The one exception is a test of these scripts. RIG_TEST_FAKES names a folder under
# the temp folder, HOME must not be the real one, and every command below must
# resolve to a regular file of that folder, not a link, whose second line is
# RIG_FAKE_MARK: a fake the test wrote, never the real binary.
RIG_HOST_COMMANDS="docker systemctl sudo fuser ip"
RIG_FAKE_MARK="# rig test fake"

rig_refuse() { echo "rig: $*. Nothing was touched." >&2; exit 2; }

rig_own_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
[ -n "$rig_own_home" ] || rig_refuse "this user has no home in the password database"
if [ -n "${RIG_TEST_FAKES:-}" ]; then
    [ "$HOME" != "$rig_own_home" ] || rig_refuse "a test run needs a HOME of its own"
    rig_temp="$(realpath -e "${TMPDIR:-/tmp}" 2>/dev/null || true)"
    rig_fakes="$(realpath -e "$RIG_TEST_FAKES" 2>/dev/null || true)"
    [ -n "$rig_temp" ] && [ "$rig_temp" != / ] && [ -n "$rig_fakes" ] && [ "$rig_fakes" = "$RIG_TEST_FAKES" ] \
        && [ "${rig_fakes#"$rig_temp"/}" != "$rig_fakes" ] \
        || rig_refuse "a test run's fakes must be a folder under the temp folder, and $RIG_TEST_FAKES is not"
    for rig_tool in $RIG_HOST_COMMANDS; do
        rig_path="$(command -v "$rig_tool" || true)"
        [ "$rig_path" = "$RIG_TEST_FAKES/$rig_tool" ] \
            || rig_refuse "a test run found $rig_tool outside $RIG_TEST_FAKES"
        [ -f "$rig_path" ] && [ ! -L "$rig_path" ] && [ "$(sed -n 2p "$rig_path" 2>/dev/null)" = "$RIG_FAKE_MARK" ] \
            || rig_refuse "a test run found $rig_path, which is not a fake the test wrote"
    done
else
    [ "$HOME" = "$rig_own_home" ] || rig_refuse "HOME is $HOME, and this user's home is $rig_own_home"
fi

rig_marker="$HOME/.otodock-rig/this-host-is-a-rig"
[ -f "$rig_marker" ] || rig_refuse "no $rig_marker: this host is not a rig"
rig_want="$(uname -n) $(cat /etc/machine-id)"
[ "$(tr -s ' \n' ' ' < "$rig_marker" | sed 's/ $//')" = "$rig_want" ] \
    || rig_refuse "$rig_marker does not name this host ($rig_want)"
