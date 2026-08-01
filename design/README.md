# design/

As-built solution design for the Sovereign MCP Platform.

| File | Tracked | What it is |
|---|---|---|
| `index.html` | yes | **The source, and the deliverable.** Self-contained: the diagram is inline SVG, so it opens from disk with no network access and no sibling files. |
| `architecture.svg` | yes | **Derived, but committed.** Extracted from `index.html` so GitHub previews the diagram and the root README can embed it. |
| `architecture.png` | no | Rasterised from the extracted SVG. Generate on demand. |

Open it directly:

```
file:///M:/MCP_Project/design/index.html
```

## One source, one derived copy

The diagram used to live in its own `.svg` with the page referencing it. Once the
page had to be self-contained, that would have meant the same markup authored in
two places, with nothing to announce when they drifted apart.

So the direction is inverted. `index.html` holds the only **authored** copy;
`scripts/render-design.sh` extracts the `<svg>` element from it and writes
`architecture.svg`. Nothing is hand-edited in the extracted file.

`architecture.svg` is committed anyway, deliberately: GitHub renders a `.svg` in
its file view and in README markdown, but will not render `.html`. Without it
the diagram is invisible in the web UI, which is where most people meet the
repository.

> **Consequence.** A derived file under version control only stays honest if it
> is regenerated. After editing the diagram, run the render script and commit
> **both** `index.html` and `architecture.svg` in the same commit. A diff that
> touches only one of them is a mistake.

## Regenerating the artefacts

```bash
wsl -d DockerEngine
bash /mnt/m/MCP_Project/scripts/render-design.sh
```

Produces `architecture.svg` (extracted, **commit this**) and `architecture.png`
at 3120x2613 — 2x the 1600pt canvas. Pass a width to override:

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
4. Commit `index.html` **and** the regenerated `architecture.svg` together, so
   the copy GitHub previews matches the page.
