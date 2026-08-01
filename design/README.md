# design/

As-built architecture for the Sovereign MCP Platform.

| File | Tracked | What it is |
|---|---|---|
| `architecture.svg` | yes | **Authoritative source.** Hand-authored; edit this. |
| `index.html` | yes | Design page — embeds the SVG plus the as-built summary, verification evidence, and open discrepancies. |
| `architecture.png` | **no** | Build artefact rendered from the SVG. Gitignored. |

## Why the PNG is not tracked

It is derived, not authored. Committing it would mean a ~790 KB binary changing
on every diagram edit, with no reviewable diff and a standing risk of the raster
silently drifting out of sync with the SVG it claims to depict. The SVG renders
in any browser and in GitHub's own preview, so nothing is lost by generating the
raster on demand.

Generate it when you need one — for a slide, a PDF, or anywhere SVG is awkward.

## Regenerating the PNG

```bash
wsl -d DockerEngine
bash /mnt/m/MCP_Project/scripts/render-design.sh
```

Output: `design/architecture.png` at 3120x2430 (2x the 1560pt canvas). Pass a
width to override:

```bash
bash scripts/render-design.sh 1560     # 1x
bash scripts/render-design.sh 6240     # 4x, for print
```

The script installs `librsvg2-bin` and `fonts-dejavu-core` on first run if they
are missing. Both land inside the DockerEngine distro's vhdx on `M:\`, so no
rendering toolchain is installed on `C:\` (resource-governance.md section 1).

**Fonts are not optional.** Without `fonts-dejavu-core`, `rsvg-convert` produces
a PNG in which every text element is blank — boxes and arrows render fine, so
the failure is easy to miss unless you actually open the output. The SVG's
`font-family` list starts with `DejaVu Sans` to match what the container has.

## After editing the SVG

1. Re-render and **open the PNG** — do not trust the exit code. Text overflow,
   label collisions and truncated strings are all silent failures in SVG: the
   file stays valid and `rsvg-convert` still returns 0.
2. Check the values that go stale. The diagram hardcodes the WSL host address
   (`172.28.64.1`, which can change after a reboot or `wsl --shutdown`) and the
   three image sizes, which move whenever dependencies change.
3. Keep `index.html` in step — it restates several of the same figures.
