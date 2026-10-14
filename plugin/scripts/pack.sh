#!/usr/bin/env bash
# Build the plugin and assemble the ApplicationPlugins bundle.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/c/Program Files/dotnet:$PATH"

dotnet build "$here/src/AcadMcp.Plugin" --nologo -v q

out="$here/bundle/AcadMcp.bundle/Contents/Windows/2026"
mkdir -p "$out"
cp "$here/src/AcadMcp.Plugin/bin/Debug/AcadMcp.Plugin.dll" "$out/"
cp "$here/src/AcadMcp.Plugin/bin/Debug/AcadMcp.Core.dll"   "$out/"
echo "bundle assembled: $here/bundle/AcadMcp.bundle"
