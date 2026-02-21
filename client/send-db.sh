#!/bin/sh
set -u

SERVER_BASE="http://kobo-server.local:8000"

NOTES="/mnt/onboard/.adds/notes"
DB="/mnt/onboard/.kobo/KoboReader.sqlite"
CURL="${NOTES}/curl"
CERT="${NOTES}/ca-bundle.crt"
SQLITE="${NOTES}/sqlite3"

SERVER_URL="${SERVER_BASE}/upload"
SERVER_HEALTH="${SERVER_BASE}/health"
STATE_FILE="${NOTES}/last_sync.txt"
STATUS_FILE="${NOTES}/last_status.txt"
ERROR_FILE="${NOTES}/last_error.txt"
UPLOAD_DB="${NOTES}/upload.sqlite"


LD_LIBRARY_PATH="${NOTES}/lib:${LD_LIBRARY_PATH:-}"
export LD_LIBRARY_PATH

LAST_ERROR=""

on_exit() {
  code=$?
  if [ "$code" -ne 0 ] && [ -z "$LAST_ERROR" ]; then
    echo "Unexpected error" > "$ERROR_FILE"
    echo "Failed: Unexpected error" > "$STATUS_FILE"
  fi
}

trap on_exit EXIT

fail() {
  LAST_ERROR="$1"
  echo "$LAST_ERROR" > "$ERROR_FILE"
  echo "Failed: $LAST_ERROR" > "$STATUS_FILE"
  echo "ERROR: $LAST_ERROR" >&2
  exit 1
}

mkdir -p "$NOTES" || fail "Cannot create notes directory"

if [ ! -f "$DB" ]; then
  fail "Database not found: $DB"
fi

if [ -f "$STATE_FILE" ]; then
  LAST_SYNC=$(cat "$STATE_FILE")
else
  LAST_SYNC="1970-01-01T00:00:00"
fi

rm -f "$UPLOAD_DB"

if ! "$SQLITE" "$UPLOAD_DB" <<EOF
ATTACH '$DB' AS k;
CREATE TABLE Bookmark AS
SELECT * FROM k.Bookmark
WHERE (DateModified IS NOT NULL AND DateModified > '$LAST_SYNC')
   OR (DateCreated IS NOT NULL AND DateCreated > '$LAST_SYNC');
CREATE TABLE content AS
SELECT * FROM k.content
WHERE ContentID IN (
  SELECT ContentID FROM Bookmark
  UNION
  SELECT VolumeID FROM Bookmark
  UNION
  SELECT ContentID || '-1' FROM Bookmark
);
DETACH k;
EOF
then
  fail "Failed to build upload database"
fi

COUNT=$("$SQLITE" "$UPLOAD_DB" "SELECT COUNT(*) FROM Bookmark;") || fail "Failed to count highlights"
if [ "$COUNT" -eq 0 ]; then
  echo "No new highlights to upload."
  echo "No new highlights to upload." > "$STATUS_FILE"
  : > "$ERROR_FILE"
  exit 0
fi

CURL_CERT_ARGS=""
if [ -f "$CERT" ]; then
  CURL_CERT_ARGS="--cacert $CERT"
fi

if ! $CURL $CURL_CERT_ARGS -f -s -S "$SERVER_HEALTH" > /dev/null; then
  fail "Server not reachable: $SERVER_BASE"
fi

if ! $CURL $CURL_CERT_ARGS -f -s -S -X POST "$SERVER_URL" -F "db=@$UPLOAD_DB" > /dev/null; then
  fail "Upload failed"
fi

NOW=$(date -u "+%Y-%m-%dT%H:%M:%S")
echo "$NOW" > "$STATE_FILE"
echo "Uploaded $COUNT highlights at $NOW" > "$STATUS_FILE"
: > "$ERROR_FILE"
echo "Uploaded $COUNT highlights."