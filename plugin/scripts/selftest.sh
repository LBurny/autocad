#!/usr/bin/env bash
#
# Build the plugin, install the bundle, and verify it in a scratch AutoCAD.
#
# Two suites, either or both in one launch:
#
#   --engine   the plugin's own self-test, run inside AutoCAD at startup
#              (ACADMCP_SELFTEST=1): apply, journal round-trip, rollback, replay,
#              the Ctrl+Z interaction, and that Chinese text gets the CJK style.
#   --rpc      tests/live_rpc_check.py against the running plugin, over the real
#              socket, using this repository's own client, backend and journal
#              control. This is what proves the wire format and the two sides
#              agree.
#
# Why the loop looks like this: a .NET assembly cannot be unloaded from AutoCAD,
# so a running instance is always holding the previous build. Every change
# therefore needs a fresh AutoCAD, and the only way that is bearable is to make
# the whole cycle unattended.
#
# Three failure modes cost real time before they were handled here, so each is now
# diagnosed explicitly instead of looking like a timeout:
#
#   * the "Security - Unsigned Executable" prompt. SECURELOAD is 1 and
#     TRUSTEDPATHS was empty, so AutoCAD blocked the load and the log file never
#     appeared at all. scripts/trust-autocad.ps1 fixes that before every run,
#     because each rebuild produces a new unsigned DLL and the prompt would come
#     back every time.
#   * a modal dialog of any kind leaves the plugin unloaded and the log empty.
#     scripts/acad-state.ps1 reports whether the AutoCAD frame is disabled, which
#     is the reliable signal for that.
#   * the report marker line carries a timestamp, so an awk pattern anchored at
#     the start of the line matches nothing and the report comes out empty.
#
# Usage:
#   scripts/selftest.sh              # both suites, refuses to close a running AutoCAD
#   scripts/selftest.sh --engine     # the in-AutoCAD self-test only
#   scripts/selftest.sh --rpc        # the live RPC exchange only
#   scripts/selftest.sh --force      # close a running AutoCAD first
#   scripts/selftest.sh --keep       # leave the scratch AutoCAD running
#   scripts/selftest.sh --wait 300   # seconds to wait for each report (default 240)
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="$(cd "$here/.." && pwd)"
acad="${ACAD_DIR:-F:/AutoCAD/AutoCAD 2026}/acad.exe"
plugins="$HOME/AppData/Roaming/Autodesk/ApplicationPlugins"
log="$HOME/AppData/Local/Temp/acadmcp-plugin.log"
force=0
keep=0
wait_s=240
mode=all

while [ $# -gt 0 ]; do
  case "$1" in
    --engine) mode=engine; shift ;;
    --rpc) mode=rpc; shift ;;
    --all) mode=all; shift ;;
    --force) force=1; shift ;;
    --keep) keep=1; shift ;;
    --wait) wait_s="${2:?--wait needs a number of seconds}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ ! -x "$acad" ]; then
  echo "AutoCAD not found at $acad — set ACAD_DIR" >&2
  exit 2
fi

if [ "$mode" = "all" ]; then
  run_engine=1
  run_rpc=1
else
  run_engine=$([ "$mode" = "engine" ] && echo 1 || echo 0)
  run_rpc=$([ "$mode" = "rpc" ] && echo 1 || echo 0)
fi

# Prefer the project's venv, which has the package installed editable. The live
# check itself needs only the standard library, so a bare python will do too.
python_exe="$root/.venv/Scripts/python.exe"
[ -x "$python_exe" ] || python_exe=python

acad_running() {
  powershell -NoProfile -Command \
    "@(Get-Process acad -ErrorAction SilentlyContinue).Count" 2>/dev/null | tr -d '[:space:]'
}

wait_for_marker() {  # marker, description
  local marker="$1" what="$2" i
  for i in $(seq 1 $(( wait_s / 2 ))); do
    if [ -f "$log" ] && grep -q "$marker" "$log" 2>/dev/null; then
      return 0
    fi
    # From 15s in, if there is still no log at all, find out whether AutoCAD is
    # simply blocked. Checking every 15s keeps this cheap: the probe spawns
    # PowerShell and enumerates windows.
    if [ ! -f "$log" ] && [ $(( i % 7 )) -eq 0 ] && [ "$(acad_running)" != "0" ]; then
      local state
      state=$(powershell -NoProfile -ExecutionPolicy Bypass -File "$here/scripts/acad-state.ps1" 2>/dev/null || true)
      if printf '%s' "$state" | grep -q "enabled=False"; then
        echo >&2
        echo "AutoCAD is blocked by a modal dialog, so the plugin never loaded. Windows seen:" >&2
        printf '%s\n' "$state" | grep '^window=' >&2 || true
        echo >&2
        echo "If that is the unsigned-DLL security prompt, trust the folder and retry:" >&2
        echo "  powershell -File scripts/trust-autocad.ps1   (with AutoCAD closed)" >&2
        return 2
      fi
    fi
    sleep 2
  done
  echo "waited ${wait_s}s for $what and never saw it" >&2
  return 1
}

cleanup() {
  if [ "$keep" != "1" ]; then
    powershell -NoProfile -Command "Stop-Process -Name acad -Force -ErrorAction SilentlyContinue" || true
  fi
}

running=$(acad_running)
if [ "${running:-0}" != "0" ] && [ "$force" != "1" ]; then
  echo "AutoCAD is running ($running instance(s))." >&2
  echo "A running AutoCAD holds the previous build and cannot reload it." >&2
  echo "Save your drawings and close it, or re-run with --force to close it now." >&2
  exit 1
fi

if [ "${running:-0}" != "0" ]; then
  echo "closing $running AutoCAD instance(s) ..."
  powershell -NoProfile -Command "Stop-Process -Name acad -Force -ErrorAction SilentlyContinue" || true
  for _ in $(seq 1 40); do
    [ "$(acad_running)" = "0" ] && break
    sleep 0.5
  done
  if [ "$(acad_running)" != "0" ]; then
    echo "AutoCAD did not exit; aborting rather than deleting DLLs it still holds." >&2
    exit 1
  fi
  # AutoCAD writes its profile back to the registry as it exits, so the trust
  # setting below has to be written after it is really gone.
  sleep 2
fi

# Must happen while AutoCAD is closed, and before the launch, or an unsigned
# rebuild will raise a prompt that stops the plugin from loading.
echo "making the plugin folder trusted ..."
if ! powershell -NoProfile -ExecutionPolicy Bypass -File "$here/scripts/trust-autocad.ps1"; then
  echo "warning: could not confirm the trusted path; AutoCAD may show a security prompt." >&2
fi

echo "building and installing the bundle ..."
"$here/scripts/pack.sh" >/dev/null
rm -rf "$plugins/AcadMcp.bundle"
mkdir -p "$plugins"
cp -r "$here/bundle/AcadMcp.bundle" "$plugins/"

rm -f "$log"
if [ "$run_engine" = "1" ]; then
  echo "launching a scratch AutoCAD with the self-test armed (waiting up to ${wait_s}s) ..."
  launch_env="\$env:ACADMCP_SELFTEST='1';"
else
  echo "launching a scratch AutoCAD (waiting up to ${wait_s}s) ..."
  launch_env=""
fi
powershell -NoProfile -Command \
  "$launch_env Start-Process -FilePath '$acad' -ArgumentList '/nologo'" >/dev/null

started=$(date +%s)
elapsed() { echo $(( $(date +%s) - started )); }

status=0

if [ "$run_engine" = "1" ]; then
  if ! wait_for_marker "AUTO-SELFTEST-DONE" "the self-test report"; then
    echo "--- full log ---" >&2
    [ -f "$log" ] && cat "$log" >&2
    cleanup
    exit 1
  fi
  echo "engine self-test: $(elapsed)s"

  # The report is everything after the marker line the engine writes. Timestamped
  # lines after it are dropped, which also removes AUTO-SELFTEST-DONE. `|| true`
  # matters: when the slice is empty grep exits 1 and `set -e` would kill the
  # script before it could print anything.
  report=$(awk '/self-test report$/{found=1; next} found' "$log" \
    | grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2} ' || true)
  printf '%s\n' "$report"

  if printf '%s' "$report" | grep -q "CHECK(S) FAILED"; then
    echo "--- full log ---"
    cat "$log"
    status=1
  elif ! printf '%s' "$report" | grep -q "all checks passed"; then
    echo "the engine run did not finish cleanly; full log:"
    cat "$log"
    status=1
  fi
fi

if [ "$run_rpc" = "1" ]; then
  # The listener binds during Initialize, before the self-test runs, so this
  # marker appears early. In --all mode the wait above has already happened, and
  # waiting for it again is instant.
  if ! wait_for_marker "rpc listening" "the RPC listener"; then
    echo "--- full log ---" >&2
    [ -f "$log" ] && cat "$log" >&2
    cleanup
    exit 1
  fi
  echo "live RPC check: $(elapsed)s"
  if rpc_out=$("$python_exe" "$root/tests/live_rpc_check.py" 2>&1); then
    rpc_status=0
  else
    rpc_status=$?
  fi
  printf '%s\n' "$rpc_out"
  [ "$rpc_status" != "0" ] && status=1
fi

cleanup
exit "$status"
