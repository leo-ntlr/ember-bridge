#!/bin/bash
# Ember Bridge: every command in one place (run from anywhere).
#   bash ember.sh             install, check the FIRMS key, start (or resume) all downloads in the background
#   bash ember.sh status      progress of the downloads and of the build
#   bash ember.sh build       turn the downloads into the harmonized calendar + web data (background, ~1-2 h)
#   bash ember.sh serve       open the web app on http://localhost:8000
#   bash ember.sh update      fetch the newest days, then refresh the current year (model kept)
#   bash ember.sh stop        stop what runs in the background (running the same command again resumes)
#   bash ember.sh test        run the test suite (no network needed)
# The FIRMS MAP_KEY is asked once (typing stays hidden) and kept in .env, on this computer only.
set -u
cd "$(dirname "$0")" || exit 1
mkdir -p logs data/raw
DL_PID=logs/download.pid
BUILD_PID=logs/build.pid
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
AWAKE=""
command -v caffeinate >/dev/null 2>&1 && AWAKE="caffeinate -ims"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

setup() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is missing: install it from https://www.python.org/downloads/ then run this again."; exit 1
  fi
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Python 3.9 or newer is needed (python.org)."; exit 1
  fi
  if [ ! -x .venv/bin/python ]; then
    echo "Creating the Python environment..."
    python3 -m venv .venv || { echo "Could not create the .venv folder."; exit 1; }
  fi
  PY=.venv/bin/python
  echo "Installing the Python packages (1 to 3 minutes the first time)..."
  if ! "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt; then
    echo "    Warning: the package install failed (the downloads still work, the build needs it)."
    return 1
  fi
}

ask_key() {
  if [ -z "${FIRMS_MAP_KEY:-}" ] && ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?FIRMS_MAP_KEY=.+' .env 2>/dev/null; then
    printf "Paste your FIRMS MAP_KEY (nothing shows while you paste, that is normal), then press Enter: "
    IFS= read -r -s KEY
    echo
    KEY=$(printf '%s' "$KEY" | tr -d "[:space:]\"'")
    if [ -z "$KEY" ]; then echo "No key entered."; exit 1; fi
    (umask 077; printf 'FIRMS_MAP_KEY=%s\n' "$KEY" > .env)
    unset KEY
    echo "    Key saved in .env, on this computer only."
  fi
}

launch_downloads() {
  nohup $AWAKE "$PY" -u -m ember_bridge.firms_api all --workers 4 --pid-file "$DL_PID" >> logs/download.log 2>&1 &
  sleep 20
  echo; tail -n 4 logs/download.log; echo
  if alive "$DL_PID"; then
    echo "Downloads are running in the background (log: logs/download.log). You can close this window."
    echo "Keep the Mac plugged in with the lid open. Progress: bash ember.sh status"
  else
    echo "The download stopped right away: read the lines above (full log: logs/download.log)."
  fi
}

launch_build() {
  if alive "$BUILD_PID"; then echo "A build is already running: bash ember.sh status"; return; fi
  nohup $AWAKE "$PY" -u -m ember_bridge.build --pid-file "$BUILD_PID" "$@" >> logs/build.log 2>&1 &
  sleep 5
  tail -n 3 logs/build.log
  echo "The build runs in the background (log: logs/build.log). Progress: bash ember.sh status"
}

case "${1:-start}" in
  start)
    if alive "$DL_PID"; then echo "Downloads are already running."; "$PY" -m ember_bridge.firms_api status --pid-file "$DL_PID"; exit 0; fi
    setup
    ask_key
    echo "Checking the key with FIRMS..."
    if ! "$PY" -m ember_bridge.firms_api check; then
      echo
      echo "The check failed (message above). If this key already worked, keep .env: FIRMS may block it for a"
      echo "while after too many requests, so wait 30 to 60 minutes and run bash ember.sh again."
      echo "Only if the key never worked: rm .env, then bash ember.sh"
      exit 1
    fi
    FREE_GB=$(df -Pk . | awk 'NR==2 {print int($4/1048576)}')
    echo "Free disk space: ${FREE_GB} GB (downloads about 15 GB, processing about 15 GB more)."
    [ "${FREE_GB:-0}" -lt 20 ] && echo "    Warning: little space left. If the disk fills up, downloads stop cleanly and resume later."
    launch_downloads ;;
  status)
    "$PY" -m ember_bridge.firms_api status --pid-file "$DL_PID"
    if [ -f logs/download.log ]; then
      echo; echo "Last lines of logs/download.log:"; tail -n 3 logs/download.log
    fi
    if [ -f logs/build.log ]; then
      echo; echo "Build: $(alive "$BUILD_PID" && echo running || echo not running). Last lines of logs/build.log:"
      tail -n 6 logs/build.log
    fi ;;
  build)
    shift
    setup || exit 1
    if alive "$DL_PID"; then echo "Note: downloads are still running; the build uses what is on disk now."; fi
    launch_build "$@" ;;
  update)
    alive "$DL_PID" && { echo "Downloads are still running: wait for them, then run update."; exit 1; }
    "$PY" -u -m ember_bridge.firms_api all --workers 4 --pid-file "$DL_PID" 2>&1 | tail -n 5
    launch_build --years "$(date +%Y)" ;;
  serve)
    [ -f web/data/meta.json ] || { echo "No web data yet: run bash ember.sh build first."; exit 1; }
    echo "Web app on http://localhost:8000 (Ctrl-C to stop)."
    (sleep 1; command -v open >/dev/null 2>&1 && open http://localhost:8000) &
    exec "$PY" -m ember_bridge.serve --port 8000 --bind 127.0.0.1 --directory web ;;
  stop)
    for f in "$DL_PID" "$BUILD_PID"; do
      if alive "$f"; then kill "$(cat "$f")" && echo "Stopped $(basename "$f" .pid)."; rm -f "$f"; fi
    done ;;
  test)
    for t in ingest harmonize analytics events firms_api build serve; do
      echo "== $t"
      if OUT=$("$PY" tests/test_$t.py 2>&1); then echo "$OUT" | tail -n 1; else echo "$OUT" | tail -n 25; echo "FAILED: $t"; exit 1; fi
    done
    echo "All tests passed." ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
