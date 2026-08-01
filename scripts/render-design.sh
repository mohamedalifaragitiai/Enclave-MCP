#!/usr/bin/env bash
#
# Render design/architecture.svg to a PNG.
#
# The SVG is the authoritative source and is tracked in git; the PNG is a build
# artefact and is not (see .gitignore). Regenerate it whenever the SVG changes,
# or whenever you need a raster for a slide or a document.
#
# Run inside the DockerEngine distro, so the rendering toolchain installs into
# that distro's vhdx on M:\ and never touches C:\ (resource-governance.md
# section 1):
#
#     wsl -d DockerEngine
#     bash /mnt/m/MCP_Project/scripts/render-design.sh
#
# Optional first argument overrides the output width in pixels (default 3120,
# which is 2x the SVG's 1560pt canvas).
set -euo pipefail

WIDTH="${1:-3120}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESIGN_DIR="$(cd "$SCRIPT_DIR/../design" && pwd)"

SVG="$DESIGN_DIR/architecture.svg"
PNG="$DESIGN_DIR/architecture.png"

if [ ! -f "$SVG" ]; then
    echo "error: $SVG not found" >&2
    exit 1
fi

# librsvg2-bin provides rsvg-convert; without a font package it renders every
# text element as blank, so fonts-dejavu-core is not optional. The SVG's
# font-family list starts with DejaVu Sans for exactly this reason.
if ! command -v rsvg-convert >/dev/null 2>&1; then
    echo "installing librsvg2-bin + fonts-dejavu-core into this distro ..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq librsvg2-bin fonts-dejavu-core 2>&1 | tail -2
fi

rsvg-convert -w "$WIDTH" "$SVG" -o "$PNG"

echo "rendered: $PNG"
file "$PNG"
