#!/usr/bin/env bash
#
# Rasterise the solution-design diagram.
#
# design/index.html is the single self-contained source: the diagram lives in it
# as inline SVG, so the page renders with no network access and no sibling
# files. This script extracts that SVG and rasterises it, which means the PNG
# cannot drift from the page the way a separately-maintained copy would.
#
# Both outputs are build artefacts and are gitignored:
#     design/architecture.svg   extracted, useful for embedding elsewhere
#     design/architecture.png   rasterised
#
# Run inside the DockerEngine distro, so the rendering toolchain installs into
# that distro's vhdx on M:\ and never touches C:\ (resource-governance.md
# section 1):
#
#     wsl -d DockerEngine
#     bash /mnt/m/MCP_Project/scripts/render-design.sh
#
# Optional first argument overrides the output width in pixels (default 3120,
# which is 2x the SVG's 1600pt canvas).
set -euo pipefail

WIDTH="${1:-3120}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESIGN_DIR="$(cd "$SCRIPT_DIR/../design" && pwd)"

HTML="$DESIGN_DIR/index.html"
SVG="$DESIGN_DIR/architecture.svg"
PNG="$DESIGN_DIR/architecture.png"

if [ ! -f "$HTML" ]; then
    echo "error: $HTML not found" >&2
    exit 1
fi

# librsvg2-bin provides rsvg-convert; without a font package it renders every
# text element as blank, so fonts-dejavu-core is not optional. The SVG's
# font-family list includes DejaVu Sans for exactly this reason.
if ! command -v rsvg-convert >/dev/null 2>&1; then
    echo "installing librsvg2-bin + fonts-dejavu-core into this distro ..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq librsvg2-bin fonts-dejavu-core 2>&1 | tail -2
fi

# Pull the single <svg> element out of the page. There is exactly one, and the
# opening tag starts a line, so a range match is sufficient and avoids needing
# an XML parser here.
sed -n '/^<svg /,/^<\/svg>/p' "$HTML" > "$SVG"

if [ ! -s "$SVG" ]; then
    echo "error: no <svg> element found in $HTML" >&2
    exit 1
fi
if ! grep -q '</svg>' "$SVG"; then
    echo "error: extracted SVG is truncated - no closing tag" >&2
    exit 1
fi

rsvg-convert -w "$WIDTH" "$SVG" -o "$PNG"

echo "extracted: $SVG"
echo "rendered : $PNG"
file "$PNG"
