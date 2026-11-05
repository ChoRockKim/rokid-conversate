#!/usr/bin/env bash
# Build a release APK signed with the fixed key and (optionally) upload it to the server for OTA.
# The glasses pick it up by themselves on the next connection (or within 6 h) and install it.
#
#   scripts/release.sh <versionCode> <versionName> [--upload]
#
# Environment (values never go into the repository):
#   RELEASE_KEYSTORE        keystore file (alias "conversate"). Created on first run if missing.
#   RELEASE_KEYSTORE_PASS   its password
#   OTA_UPLOAD_TOKEN        for --upload (Railway variable of the same name)
#   CONVERSATE_SERVER       default https://server-production-24bc.up.railway.app
#
# Keep the keystore safe: losing it means the glasses can no longer be updated over the air
# (the app would have to be uninstalled and installed again over adb).
set -euo pipefail
cd "$(dirname "$0")/.."
CODE="${1:?versionCode (integer, larger than the installed one)}"
NAME="${2:?versionName, e.g. 0.2.0}"
UPLOAD="${3:-}"
SERVER="${CONVERSATE_SERVER:-https://server-production-24bc.up.railway.app}"
KS="${RELEASE_KEYSTORE:?RELEASE_KEYSTORE (path) required}"
PASS="${RELEASE_KEYSTORE_PASS:?RELEASE_KEYSTORE_PASS required}"

if [ ! -s "$KS" ]; then
  echo "creating release keystore at $KS (back it up!)"
  mkdir -p "$(dirname "$KS")"
  keytool -genkeypair -keystore "$KS" -storepass "$PASS" -keypass "$PASS" -alias conversate \
    -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=rokid-conversate" >/dev/null
fi

./gradlew --no-daemon -q :app:assembleRelease \
  -PVERSION_CODE="$CODE" -PVERSION_NAME="$NAME" \
  -PRELEASE_KEYSTORE="$KS" -PRELEASE_KEYSTORE_PASS="$PASS"

APK=app/build/outputs/apk/release/app-release.apk
[ -s "$APK" ] || { echo "build failed"; exit 1; }
OUT="build/rokid-conversate-$NAME.apk"
mkdir -p build
cp "$APK" "$OUT"
echo "built $OUT ($(wc -c < "$OUT") bytes)"

if [ "$UPLOAD" = "--upload" ]; then
  curl -sS --fail-with-body -X PUT "$SERVER/app/upload?versionCode=$CODE&versionName=$NAME" \
    -H "Authorization: Bearer ${OTA_UPLOAD_TOKEN:?OTA_UPLOAD_TOKEN required for --upload}" \
    -H "Content-Type: application/vnd.android.package-archive" \
    --data-binary @"$OUT"
  echo
fi
