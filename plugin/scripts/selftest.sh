#!/usr/bin/env bash
#
# Build the plugin, install the bundle, and run its self-test in a scratch
# AutoCAD — the edit-build-test loop for this plugin.
#
# Why the loop looks like this: a .NET assembly cannot be unloaded from AutoCAD,
# so a running instance is always holding the previous build. Every change
# therefore needs a fresh AutoCAD, and the only way that is bearable is to make
# the whole cycle unattended. The plugin runs its own checks at startup
# (ACADMCP_SELFTEST=1), reports to a log file, and this script reads it.
#
# Two failure modes cost real time before they were handled here, so both are
# now diagnosed explicitly instead of looking like a timeout:
#
#   * the "Security - Unsigned Executable" prompt. SECURELOAD is 1 and
#     TRUSTEDPATHS was empty, so AutoCAD blocked the load and the log file never
#     appeared at all. scripts/trust-autocad.ps1 fixes that before every run,
#     because each rebuild produces a new unsigned DLL and the prompt would come
#     back every time.
#   * a modal dialog of any kind leaves the plugin unloaded and the log empty.
#     scripts/acad-state.ps1 reports whether the AutoCAD frame is disabled, which
#     is the reliable signal for that.
#
# Usage:
#   scripts/selftest.sh              # refuses to run if AutoCAD is already open
#   scripts/selftest.sh --force      # closes it first
#   scripts/selftest.sh --keep       # leave the scratch AutoCAD running afterwards
#   scripts/selftest.sh --wait 300   # seconds to wait for the report (default 240)
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
acad="${ACAD_DIR:-F:/AutoCAD/AutoCAD 2026}/acad.exe"
plugins="$HOME/AppData/Roaming/Autodesk/ApplicationPlugins"
log="$HOME/AppData/Local/Temp/acadmcp-plugin.log"
force=0
keep=0
wait_s=240

while [ $# -gt 0 ]; do
  case "$1" in
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

acad_running() {
  powershell -NoProfile -Command \
    "@(Get-Process acad -ErrorAction SilentlyContinue).Count" 2>/dev/null | tr -d '[:space:]'
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
echo "launching a scratch AutoCAD (waiting up to ${wait_s}s) ..."
powershell -NoProfile -Command \
  "\$env:ACADMCP_SELFTEST='1'; Start-Process -FilePath '$acad' -ArgumentList '/nologo'" >/dev/null

started=$(date +%s)
elapsed() { echo $(( $(date +%s) - started )); }

done_marker=0
dialog_seen=0
ticks=$(( wait_s / 2 ))
for i in $(seq 1 "$ticks"); do
  if [ -f "$log" ] && grep -q "AUTO-SELFTEST-DONE" "$log" 2>/dev/null; then
    done_marker=1
    break
  fi
  # From 15s in, if there is still no log at all, find out whether AutoCAD is
  # simply blocked. Checking every 15s keeps this cheap: the probe spawns
  # PowerShell and enumerates windows.
  if [ ! -f "$log" ] && [ $(( i % 7 )) -eq 0 ] && [ "$(acad_running)" != "0" ]; then
    state=$(powershell -NoProfile -ExecutionPolicy Bypass -File "$here/scripts/acad-state.ps1" 2>/dev/null || true)
    if printf '%s' "$state" | grep -q "enabled=False"; then
      dialog_seen=1
      echo
      echo "AutoCAD is blocked by a modal dialog, so the plugin never loaded. Windows seen:"
      printf '%s\n' "$state" | grep '^window=' || true
      echo
      echo "If that is the unsigned-DLL security prompt, trust the folder and retry:"
      echo "  powershell -File scripts/trust-autocad.ps1   (with AutoCAD closed)"
      break
    fi
  fi
  sleep 2
done

echo "elapsed: $(elapsed)s"

cleanup() {
  if [ "$keep" != "1" ]; then
    powershell -NoProfile -Command "Stop-Process -Name acad -Force -ErrorAction SilentlyContinue" || true
  fi
}

if [ ! -f "$log" ]; then
  cleanup
  if [ "$dialog_seen" = "1" ]; then
    exit 1
  fi
  echo "no plugin log after $(elapsed)s: the bundle did not load." >&2
  echo "Check that AutodeskProduct/ProductType are set in PackageContents.xml," >&2
  echo "and that $(dirname "$plugins/AcadMcp.bundle") is in TRUSTEDPATHS." >&2
  exit 1
fi

if [ "$done_marker" != "1" ]; then
  echo "the run never reached AUTO-SELFTEST-DONE; full log:" >&2
  cat "$log" >&2
  cleanup
  exit 1
fi

# The report is everything after the marker line the engine writes. The marker
# is a PluginLog line, so it carries a timestamp prefix and must not be anchored
# at the start of the line; that anchoring is what made an earlier version of
# this script print an empty report. Timestamped lines after the marker are
# dropped, which also removes AUTO-SELFTEST-DONE. `|| true` matters: when the
# slice is empty grep exits 1 and `set -e` would kill the script before it could
# print anything.
report=$(awk '/self-test report$/{found=1; next} found' "$log" \
  | grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2} ' || true)
printf '%s\n' "$report"

if printf '%s' "$report" | grep -q "CHECK(S) FAILED"; then
  echo
  echo "--- full log ---"
  cat "$log"
  status=1
elif ! printf '%s' "$report" | grep -q "all checks passed"; then
  echo
  echo "the run did not finish cleanly; full log:"
  cat "$log"
  status=1
else
  status=0
fi

cleanup
exit "$status"
