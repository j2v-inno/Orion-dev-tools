#!/bin/sh
# Search a Laravel log inside a running container for a reference code.
# Prints every full log entry (including stack trace) that mentions the code.
#
# Usage: ./laravel_logs.sh <container_image> <ref_code> [log_name]
#   container_image  image name/tag used to find the running container (e.g. orion-app:latest)
#   ref_code         reference code to search for
#   log_name         log file in storage/logs (default: laravel.log)
#
# Env overrides:
#   LOG_DIR   log directory inside the container (default: /app/storage/logs)
#   TAIL_ONLY set to a number to only scan the last N lines of the log

set -eu

if [ $# -lt 2 ]; then
  echo "Usage: $0 <container_image> <ref_code> [log_name]" >&2
  exit 1
fi

IMAGE=$1
REF=$2
LOG_NAME=${3:-laravel.log}
LOG_DIR=${LOG_DIR:-/app/storage/logs}
LOG_PATH="$LOG_DIR/$LOG_NAME"

# Match a container by exact name first, then fall back to image.
CONTAINER=$(docker ps --filter "name=^${IMAGE}$" --format '{{.ID}}' | head -n 1)
if [ -z "$CONTAINER" ]; then
  CONTAINER=$(docker ps --filter "ancestor=$IMAGE" --format '{{.ID}}' | head -n 1)
fi
if [ -z "$CONTAINER" ]; then
  echo "No running container found for name/image '$IMAGE'" >&2
  exit 2
fi

echo "Container: $CONTAINER ($IMAGE)"
echo "Log:       $LOG_PATH"
echo "Ref code:  $REF"
echo "----------------------------------------"

if ! docker exec "$CONTAINER" test -f "$LOG_PATH"; then
  echo "Log file not found. Available logs in $LOG_DIR:" >&2
  docker exec "$CONTAINER" ls -la "$LOG_DIR" >&2 || true
  exit 3
fi

if [ -n "${TAIL_ONLY:-}" ]; then
  READ="tail -n $TAIL_ONLY $LOG_PATH"
else
  READ="cat $LOG_PATH"
fi

# Laravel entries start with "[YYYY-MM-DD HH:MM:SS]"; group continuation lines
# (stack trace) with their entry and print entries containing the ref code.
docker exec "$CONTAINER" sh -c "$READ" | awk -v ref="$REF" '
  function flush() { if (hit) printf "%s\n", entry; entry = ""; hit = 0 }
  /^\[[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}/ { flush(); entry = $0; if (index($0, ref)) hit = 1; found = 1; next }
  { entry = entry "\n" $0; if (index($0, ref)) hit = 1 }
  END { flush() }
'
