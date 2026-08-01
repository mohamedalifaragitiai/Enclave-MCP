# design/

As-built solution design for the Sovereign MCP Platform.

| File | Tracked | What it is |
|---|---|---|
| `index.html` | yes | **The source, and the deliverable.** Self-contained: the diagram is inline SVG, so it opens from disk with no network access and no sibling files. |
| `architecture.svg` | **no** | Extracted from `index.html`. Useful for embedding elsewhere. |
| `architecture.png` | **no** | Rasterised from the extracted SVG. |

Open it directly:

```
file:///M:/MCP_Project/design/index.html
```

## Why only one file is tracked

The diagram used to live in its own `.svg` with the page referencing it. Once the
page had to be self-contained, that would have meant the same markup existing in
two places — and a raster claiming to depict a diagram it had quietly fallen
behind. Neither copy would announce the drift.

So the direction is inverted: `index.html` holds the only copy, and the render
script **extracts** from it. The artefacts cannot disagree with the page,
because they are derived from it. Both are gitignored; neither is authored.

Cost worth knowing: GitHub renders a committed `.svg` in the file view but will
not render `.html`, so the diagram is no longer previewable in the web UI. If
that matters more than the single-file property, drop the `design/architecture.svg`
line from `.gitignore` and commit the extracted copy — accepting that it then
needs regenerating on every edit.

## Regenerating the artefacts

```bash
wsl -d DockerEngine
bash /mnt/m/MCP_Project/scripts/render-design.sh
```

Produces `architecture.svg` (extracted) and `architecture.png` at 3120x2613 —
2x the 1600pt canvas. Pass a width to override:

```bash
bash scripts/render-design.sh 1600     # 1x
bash scripts/render-design.sh 6240     # 4x, for print
```

The script installs `librsvg2-bin` and `fonts-dejavu-core` on first run if they
are missing. Both land inside the DockerEngine distro's vhdx on `M:\`, so no
rendering toolchain is installed on `C:\` (resource-governance.md section 1).

**Fonts are not optional.** Without `fonts-dejavu-core`, `rsvg-convert` produces
a PNG in which every text element is blank — boxes and arrows render fine and
the exit code is still 0, so the failure passes unnoticed unless you open the
output. The SVG's `font-family` list includes `DejaVu Sans` to match what the
container has.

## After editing the diagram

1. Edit the inline `<svg>` inside `index.html`. Keep the opening `<svg ` and
   closing `</svg>` at the start of their lines — the extraction is a line-range
   match, and the script fails loudly rather than silently if it cannot find them.
2. Re-render and **open the PNG**. Do not trust the exit code: text overflow,
   label collisions and truncated strings are all silent failures in SVG. The
   file stays valid and the render still succeeds.
3. Check the values that go stale. The diagram hardcodes the WSL host address
   (`172.28.64.1`, which can change after a reboot or `wsl --shutdown`) and the
   three image sizes, which move whenever dependencies change.
