#!/usr/bin/env bash
# A signed build on TestFlight, with no Apple ID typed anywhere: the archive
# is signed through the App Store Connect API key (the certificate and the
# profile are created or fetched by Xcode on the way) and uploaded by the
# export step. The three values come from the environment, never from a
# file in the repository:
#
#   OTODOCK_IOS_API_KEY_PATH   the .p8 (App Store Connect, Users and Access,
#                              Integrations, Team Keys; App Manager or Admin)
#   OTODOCK_IOS_API_KEY_ID     the key's id
#   OTODOCK_IOS_API_ISSUER_ID  the issuer id
#   OTODOCK_IOS_BUILD          0 to 99, raised to upload the same version again
#
# The archive is signed with the Apple Distribution certificate and the
# "OtoDock App Store" profile the project's Release configuration names
# (Xcode's automatic signing archives with a development profile, which
# needs a registered device, and the team has none): both come from the App
# Store Connect API with the same key and sit on the Mac, the certificate in
# a keychain codesign may use, the profile under Xcode's profiles folder
# (the page "TestFlight"). The key serves the upload.
#
# Run from anywhere; the dashboard is built and synced first. The archive
# and the export log land in ios-builds/<version>-<build>/ beside the
# checkout (OTODOCK_IOS_OUT_DIR moves them).
set -euo pipefail

: "${OTODOCK_IOS_API_KEY_PATH:?set OTODOCK_IOS_API_KEY_PATH (the .p8)}"
: "${OTODOCK_IOS_API_KEY_ID:?set OTODOCK_IOS_API_KEY_ID}"
: "${OTODOCK_IOS_API_ISSUER_ID:?set OTODOCK_IOS_API_ISSUER_ID}"
[ -s "$OTODOCK_IOS_API_KEY_PATH" ] || { echo "error: $OTODOCK_IOS_API_KEY_PATH is not a file" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dashboard="$here/.."
repo="$dashboard/.."
version="$(grep -E '^OTODOCK_VERSION=' "$repo/VERSIONS.md" | cut -d= -f2 | sed 's/-dev$//')"
build_digit="${OTODOCK_IOS_BUILD:-0}"
out="${OTODOCK_IOS_OUT_DIR:-$(cd "$repo/.." && pwd)/ios-builds}/$version-$build_digit"
mkdir -p "$out"

"$here/App/firebase-config.sh" --check "$here/App/App/GoogleService-Info.plist"

# The logs keep the key's id, the issuer id and the .p8 path out: xcodebuild
# echoes its whole command line first, and the logs are copied as evidence.
redact() {
    sed -l -e "s|$OTODOCK_IOS_API_KEY_PATH|<key path>|g" \
        -e "s|$OTODOCK_IOS_API_KEY_ID|<key id>|g" \
        -e "s|$OTODOCK_IOS_API_ISSUER_ID|<issuer id>|g"
}

echo "==> dashboard build and sync"
(cd "$dashboard" && npm run build && npx cap sync ios)

echo "==> archive (Release, signed through the API key)"
xcodebuild archive \
    -project "$here/App/App.xcodeproj" -scheme App -configuration Release \
    -destination 'generic/platform=iOS' \
    -archivePath "$out/App.xcarchive" \
    -allowProvisioningUpdates \
    -authenticationKeyPath "$OTODOCK_IOS_API_KEY_PATH" \
    -authenticationKeyID "$OTODOCK_IOS_API_KEY_ID" \
    -authenticationKeyIssuerID "$OTODOCK_IOS_API_ISSUER_ID" \
    2>&1 | redact | tee "$out/archive.log" | grep -E "error:|warning: .*signing|\*\* ARCHIVE" || true
grep -q "ARCHIVE SUCCEEDED" "$out/archive.log" || { echo "error: the archive failed, see $out/archive.log" >&2; exit 1; }

echo "==> export and upload to App Store Connect"
xcodebuild -exportArchive \
    -archivePath "$out/App.xcarchive" \
    -exportOptionsPlist "$here/App/ExportOptions.plist" \
    -exportPath "$out/export" \
    -allowProvisioningUpdates \
    -authenticationKeyPath "$OTODOCK_IOS_API_KEY_PATH" \
    -authenticationKeyID "$OTODOCK_IOS_API_KEY_ID" \
    -authenticationKeyIssuerID "$OTODOCK_IOS_API_ISSUER_ID" \
    2>&1 | redact | tee "$out/export.log" | grep -E "error:|Upload|EXPORT" || true
grep -q "EXPORT SUCCEEDED" "$out/export.log" || { echo "error: the export failed, see $out/export.log" >&2; exit 1; }
echo "==> uploaded: OtoDock $version build digit $build_digit (the receipt is in $out/export.log)"
