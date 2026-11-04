#!/usr/bin/env bash
# The Firebase client config, for push. GoogleService-Info.plist is never
# committed (git-ignored, with an .example beside it), so it is not a member
# of the target: when the file sits in App/, this run-script phase copies it
# into the bundle. A Release build without it stops, since a store build
# without push is a mistake; a Debug build runs without push. The example
# has a valid shape, so the example saved under the real name counts as no
# file: its API key lacks Google's prefix and its app id is zeros.
#
#   firebase-config.sh --check <plist>   exit 0 for a real config, 1 with the
#                                        reason otherwise (testflight.sh)
set -euo pipefail

genuine() {
    local key app_id
    key=$(/usr/libexec/PlistBuddy -c "Print :API_KEY" "$1" 2>/dev/null || true)
    app_id=$(/usr/libexec/PlistBuddy -c "Print :GOOGLE_APP_ID" "$1" 2>/dev/null || true)
    [[ "$key" == AIza* && "$app_id" != *:000000000000:* ]]
}

if [ "${1:-}" = "--check" ]; then
    file="${2:?--check needs the plist}"
    [ -s "$file" ] || { echo "error: $file is missing: a store build needs the Firebase console's file for the iOS app (push)" >&2; exit 1; }
    genuine "$file" || { echo "error: $file is the example file: a store build needs the Firebase console's file for the iOS app (push)" >&2; exit 1; }
    exit 0
fi

src="$SRCROOT/App/GoogleService-Info.plist"
dest="$TARGET_BUILD_DIR/$UNLOCALIZED_RESOURCES_FOLDER_PATH/GoogleService-Info.plist"
if [ -s "$src" ] && genuine "$src"; then
    cp "$src" "$dest"
    echo "Firebase config copied into the bundle"
elif [ "$CONFIGURATION" = "Release" ]; then
    if [ -s "$src" ]; then
        echo "error: App/GoogleService-Info.plist is the example file. A store build needs the Firebase console's file for the iOS app (push)" >&2
    else
        echo "error: A store build needs App/GoogleService-Info.plist (push)" >&2
    fi
    exit 1
else
    rm -f "$dest"
    if [ -s "$src" ]; then
        echo "the example Firebase config: this build runs without push"
    else
        echo "no Firebase config: this build runs without push"
    fi
fi
