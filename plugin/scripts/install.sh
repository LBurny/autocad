#!/usr/bin/env bash
#
# Build and install the AcadMcp plugin for the current user.
#
# Everything happens in per-user locations, so no administrator is needed: the
# bundle goes into %APPDATA%\Autodesk\ApplicationPlugins, and that folder is added
# to AutoCAD's TRUSTEDPATHS. The trust step is not optional — with SECURELOAD=1 an
# unsigned DLL in a folder AutoCAD does not trust raises the "Security - Unsigned
# Executable" prompt on every load, and every rebuild makes a new unsigned DLL, so
# the prompt would come back each time.
#
# The trust setting lives in AutoCAD's profile, which AutoCAD writes back to the
# registry as it exits, so a running AutoCAD has to be closed (or `--force` used)
# before it is written — otherwise the change is silently overwritten.
#
# Usage:
#   scripts/install.sh              # refuses to touch a running AutoCAD
#   scripts/install.sh --force      # close AutoCAD first
#   scripts/install.sh --uninstall  # remove the bundle (leaves TRUSTEDPATHS alone)
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
acad="${ACAD_DIR:-F:/AutoCAD/AutoCAD 2026}/acad.exe"
plugins="$HOME/AppData/Roaming/Autodesk/ApplicationPlugins"
bundle="$plugins/AcadMcp.bundle"
force=0
uninstall=0

while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

acad_running() {
  powershell -NoProfile -Command \
    "@(Get-Process acad -ErrorAction SilentlyContinue).Count" 2>/dev/null | tr -d '[:space:]'
}

if [ "$uninstall" = "1" ]; then
  if [ ! -d "$bundle" ]; then
    echo "nothing installed at $bundle"
    exit 0
  fi
  if [ "$(acad_running)" != "0" ] && [ "$force" != "1" ]; then
    echo "AutoCAD is running and holds the plugin's DLLs open." >&2
    echo "Close it, or re-run with --force." >&2
    exit 1
  fi
  if [ "$(acad_running)" != "0" ]; then
    powershell -NoProfile -Command "Stop-Process -Name acad -Force -ErrorAction SilentlyContinue" || true
    sleep 3
  fi
  rm -rf "$bundle"
  echo "removed $bundle"
  echo "TRUSTEDPATHS is left as it is: it is a harmless, ordinary folder to trust."
  exit 0
fi

if [ ! -x "$acad" ]; then
  echo "AutoCAD not found at $acad — set ACAD_DIR if it lives elsewhere." >&2
  exit 2
fi

running=$(acad_running)
if [ "${running:-0}" != "0" ] && [ "$force" != "1" ]; then
  echo "AutoCAD is running ($running instance(s))." >&2
  echo "Its loaded copy of the plugin cannot be replaced, and it would overwrite the" >&2
  echo "trust setting as it exits. Save your work and close it, or pass --force." >&2
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
    echo "AutoCAD did not exit; aborting rather than replacing DLLs it still holds." >&2
    exit 1
  fi
  sleep 2
fi

echo "making the plugin folder trusted ..."
if ! powershell -NoProfile -ExecutionPolicy Bypass -File "$here/scripts/trust-autocad.ps1"; then
  echo "warning: could not confirm the trusted path." >&2
  echo "AutoCAD may show a security prompt for the plugin; see scripts/trust-autocad.ps1." >&2
fi

echo "building ..."
"$here/scripts/pack.sh"

echo "installing into $plugins ..."
rm -rf "$bundle"
mkdir -p "$plugins"
cp -r "$here/bundle/AcadMcp.bundle" "$plugins/"

echo
echo "installed. Next:"
echo "  1. start AutoCAD — the plugin loads automatically"
echo "  2. type ACADMCPHELLO to open the steps palette"
echo "  3. connect the MCP server with the plugin_connect tool"
echo "  (the plugin listens on 127.0.0.1:9880; ACADMCP_PORT changes it)"
