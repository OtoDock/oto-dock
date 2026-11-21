#!/usr/bin/env bash
#
# Build a candidate's images on this host from its exported tree, reusing Docker's
# layer cache. The tree's VERSIONS.md names the tag first: the image reads its
# version there, and upgrade.sh refuses a tag older than the release it leaves.
#
#   build.sh <candidate dir> <tag>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rig/install/guard.sh
. "$HERE/guard.sh"
CANDIDATE="${1:?the candidate folder}" TAG="${2:?the tag}"
[ ! -e "$CANDIDATE/.git" ] || rig_refuse "$CANDIDATE is a git checkout, and this script rewrites its VERSIONS.md"
cd "$CANDIDATE"
sed -i "s/^OTODOCK_VERSION=.*/OTODOCK_VERSION=$TAG/" VERSIONS.md
[ -f config.env ] || (umask 077; printf 'POSTGRES_PASSWORD=%s\n' "$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" > config.env)
scripts/compose.sh build
docker image inspect "ghcr.io/otodock/otodock-proxy:$TAG" >/dev/null
# The tags a new build took leave their old images untagged. Tagged ones stay: the
# previous release's are the upgrade run's start.
docker image prune -f >/dev/null
