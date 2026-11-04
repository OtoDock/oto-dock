#!/usr/bin/env bash
# The app's version from the repository's VERSIONS.md (OTODOCK_VERSION=X.Y.Z),
# as the Android build reads it, written into the built Info.plist by a
# run-script phase: CFBundleShortVersionString carries the triple (a -dev
# suffix stripped, since Apple accepts digits and dots only) and
# CFBundleVersion is (major x 10000 + minor x 100 + patch) x 100 + the build
# digit OTODOCK_IOS_BUILD (0 to 99, 0 when unset), so 1.9.1 is 1090100. The
# digit is raised only to upload the same version to TestFlight again.
set -euo pipefail

versions="$SRCROOT/../../../VERSIONS.md"
line=$(grep -E '^OTODOCK_VERSION=' "$versions" || true)
if [[ ! "$line" =~ ^OTODOCK_VERSION=([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
    echo "error: OTODOCK_VERSION=X.Y.Z not found in $versions (the app version tracks VERSIONS.md)" >&2
    exit 1
fi
major=${BASH_REMATCH[1]}
minor=${BASH_REMATCH[2]}
patch=${BASH_REMATCH[3]}
digit="${OTODOCK_IOS_BUILD:-0}"
if [[ ! "$digit" =~ ^[0-9]{1,2}$ ]]; then
    echo "error: OTODOCK_IOS_BUILD must be 0 to 99, got '$digit'" >&2
    exit 1
fi
short="$major.$minor.$patch"
build=$(( (major * 10000 + minor * 100 + patch) * 100 + 10#$digit ))

plist="$TARGET_BUILD_DIR/$INFOPLIST_PATH"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $short" -c "Set :CFBundleVersion $build" "$plist"
echo "OtoDock $short, build $build"
