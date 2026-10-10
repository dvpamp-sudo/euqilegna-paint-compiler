# AGENTS.md

## Project Overview

Euqilegna Interactive Digital Art Studio — a FastAPI app that converts images into
paint-by-number packages. Entry point is `main:app` (uvicorn).

## Running the App

```sh
docker compose -f docker-compose.base44.yml up -d
```

- Web entry point: `http://localhost:3000`
- Health check: `GET /health`
- Live reload enabled (uvicorn `--reload`)

Note: the `/beta/*` pages are plain HTML files served as-is, so uvicorn's `--reload`
restarts the server but never refreshes an open browser tab. After editing
`beta/*.html` (e.g. `beta/color-by-number.html`) force a full browser reload, or the
old page keeps running its previous script.

## Key Architecture

- `main.py` — FastAPI app, routes, job orchestration (single ThreadPoolExecutor worker)
- `compiler_core.py` — core compilation pipeline (region detection, palette assignment, SVG vectorization)
- `compiler.py` — thin wrapper that calls `compile_artwork` from `compiler_core`
- `testing_runtime_v10.py` — SQLite-backed beta testing runtime, feedback, data dir resolution
- `canvas_progress.py` — one session's paint progress on one artwork, stored in the `canvas_sessions` table
- `admin_dashboard.py` — aggregates saved canvas sessions and beta feedback for the `/studio/admin` artist dashboard
- `studio_config.py` — app metadata, storage paths (uses `LOCALAPPDATA` on Windows, falls back to `~`)
- `artwork_profiles.py` — intelligent artwork profile definitions
- `premium_catalog.py` — premium sample catalog

## Environment Variables

- `EUQILEGNA_DATA_DIR` (optional) — overrides data storage directory; defaults to `~/.AppData/Local`
- `EUQILEGNA_BETA_CODE` (optional) — beta access code; auto-generated if not set

## Known Quirk: Indentation Sensitivity

`compiler_core.py` is ~158KB with a single large `compile_artwork` function. A prior
commit accidentally de-indented a block from function body (4 spaces) to module level
(0 spaces), causing a `NameError` at import time. Always verify indentation when editing
this file — Python accepts the broken code syntactically but it fails at runtime.

## Preview Console Noise (not the app)

The preview injects `https://app.base44.com/builder-bridge.js` into the served page — the
server renders only the page's own inline script, so any extra `<script>` in the DOM comes
from the platform. That bridge throws an uncaught `TypeError … reading 'bind'` from
`installPreviewLensNavigationObserver`, and because it is cross-origin it also surfaces as
an opaque `Script error.` in the page's `window.onerror`. Both are platform-side and cannot
be fixed here.

To confirm the canvas itself is clean, drive it in a plain headless browser against
`http://localhost:3000/beta/color-by-number` and watch `pageerror`/`window.onerror`: the
app's own script reports nothing, even with overlapping stroke animations, undo/reset races
and an opaque-origin sandboxed iframe. `main.py` serves `/favicon.ico` so the browser's
automatic request no longer logs a 404.

## Visual Fidelity Gate

`compiler_v12/validation.py` scores a compiled preview against the source image.
Tonal similarity is measured after blurring both at paint-region scale
(`TONE_SIGMA_RATIO`), not per pixel: a flat-colour paint map cannot carry
photographic texture, and a per-pixel score only passes packages with thousands
of tiny regions — exactly what the same report's region budget rejects. Busy
photos therefore could never satisfy both checks. Raw per-pixel error is still
reported as `meanAbsoluteError` (diagnostics only); `localToneError` is the value
behind the score.

## Canvas Session Progress

`beta/color-by-number.html` paints offline-first: `localStorage` stays the fast path and
every save is also mirrored into the `canvas_sessions` table through
`/api/canvas/progress`. Resume prefers whichever copy was saved later, so the canvas
shows its local state at once and only swaps to the studio copy when that one is newer.

- The session id (`…-session`) and the artwork key (`…-artwork`) live in `localStorage`.
  There is no login, so a session is resumable only while those two keys survive. The
  built-in artwork is stored as `dahlia-mandala`; uploads use their compilation job id.
- Painting, undoing and resetting all save automatically (debounced); "Save Now" saves
  immediately and reports whether the studio copy accepted it.
- Finishing a picture keeps it, too. When the last region is painted the canvas saves the
  finished painting to the studio's painting library through
  `POST /api/canvas/progress/<session>/complete` (multipart: `artworkKey`, `regionTotal`,
  `painted`, `colorCount` and the painted board as `image`), and the **Save to My
  Paintings** button asks for the same save on demand. The request carries the canvas's own
  `painted` count because the last region's debounced progress save may still be in flight;
  without it the endpoint's 409 ("Finish every region…") would fire on a finished board.
- The finished board travels as an image: `boardImage` copies the SVG with its styling made
  explicit (a copied SVG does not carry the page's stylesheet, so `.zone` fills, strokes and
  the `#paintGrain` filter are written onto the copy) and the browser draws it to a PNG. It
  is stored as `canvas_finished.png` — beside the package for a compiled artwork, and under
  `.data/canvas/<artwork key>/` for one that has no package (the built-in design) — and
  `PAINTING_PREVIEW_FILES` now lists it first, so "My Paintings" shows the artwork the
  painter actually completed rather than the source upload.
- `/api/paintings/<id>/preview` serves that file; with `?w=` it also serves a downscaled
  JPEG built with Pillow and cached in memory (`main.preview_thumbnail`, keyed by the source
  file's mtime, so a repainted artwork is never served stale). "My Paintings" asks for
  `?w=480`: the full-resolution previews are ~2 MB each and loading all thirteen made the
  dashboard decode over 100 MB of pixels, which stalls the preview iframe (whole gallery ≈27 MB
  before, under 1 MB as thumbnails). Keep the `?w=` in `studio/dashboard.html`.
- One painting per artwork, so finishing twice updates instead of duplicating: a compiled
  upload or design already has its `paintings` row (matched by `package_dir`) and this marks
  it `completed`, while the built-in `dahlia-mandala` gets its row on the first finish.
  `canvas_artwork_names` matches a canvas artwork by that same folder, so the built-in is one
  row on the artist dashboard (its painting, with the sessions folded in) instead of two.
- Inspect a saved session without the UI:
  `curl -s localhost:3000/api/canvas/progress/<session-id>`
- `/beta/admin-v10` lists every saved session (session, artwork, painted regions, saves,
  last saved) beneath the feedback table.

## Drawing Tools (medium picker)

`beta/color-by-number.html` ships nine media in the `TOOLS` array — three markers (Broad
Marker, Brush Marker, Fineliner; the studio default, so `TOOLS[0]` and the family listed
first in `renderToolbox`), three crayons (Wax Crayon, Soft Pastel, Metallic) and three
colored pencils (Colored Pencil, Soft Core, Watercolor) — picked from the "Drawing Tool"
section of the panel. Markers use `tip:'marker'` (`markerTip`) and draw flat, barely grained
fills: broad overlapping bands instead of wax speckle. Every medium now finishes the same way
at the edge: `.zone.done` keeps the page's own thin dark ink line (`stroke:var(--ink)` at
`--zone-stroke`) over the painted fill, the way a finished colored-pencil drawing is finally
defined by its outlines — there is no longer a `markers` board class or a white/stroke split
between families. A tool choice is stored in
`localStorage` under `<STORAGE_KEY>-tool` and is deliberately device-local: it is not part of
the snapshot mirrored to `canvas_sessions`.

Each entry is data, not code. `pattern(hex,u)` is the dried texture a finished zone keeps, and
the colored-pencil patterns are the reference look: an opaque base wash under parallel hatching
(`hatch`) plus a perpendicular pass (`crossHatch`) in a second shade, so the under-colour shows
through the over-colour. `hatch` draws horizontal bands and `crossHatch` vertical ones; both
overshoot the tile by `.08`, so they tile seamlessly — do not rotate hatch content inside a
pattern (a rotated tile leaves uncovered corners and seams).

`grain` parameterises the `#paintGrain` filter, and `grain.strength` is the single knob: the
paper tooth a dried region keeps (0 = flat ink, higher = more tooth). `buildPaintDefs` now spends
it in two places, both centred on 1 so the pigment keeps its body: `toothAmp`/`toothBase` shade
the colour (`feColorMatrix` → `feComposite operator="in"` against `SourceGraphic` → `feBlend
mode="multiply"`), and `toothAlpha`/`toothFloor` thin the alpha only a little (`strength*.5`,
floor `1-strength*.75`). Multiply is deliberate but only safe *after* the shade is masked to the
source: `feBlend` over a fully opaque backdrop raises alpha to 1 across the whole filter region,
which paints an opaque rectangle over the board. The old single-mask version (`strength` ×3, then
`1-1.5*strength`) spent everything on opacity and read as static/speckle, so markers sit lowest
(.18–.26) and crayons highest (.46–.54). `stroke` drives `sweepPaint`: pass
width/opacity/density, hatching direction (`alternate: false` = pencils hatch one way),
ragged stroke ends (`rag`), a gloss line along every pass (`sheen`, metallic), a flood of
colour before the strokes settle (`wash`, watercolor). `tip` + `tipScale` pick the crayon or
sharpened-pencil tip drawn under the hand.

The board defs are `#paintDefs` (`#paint-N` patterns, `#paintGrain`) and the CSS rule
`.zone.done` references that filter id. They must be rebuilt — `buildPaintDefs` does — after a
tool change or a palette change, and must exist *before* a zone gains `.done`, otherwise the
dangling filter reference hides the zone.

Toolbox chips are real previews, not icons: `buildSampleDefs` writes `#sample-<tool>-0/1`
patterns built from the current palette into the `#toolSampleDefs` svg, so the chips re-render
with each uploaded artwork's palette (`renderToolbox` is called from `renderCompiled`).

## Artist Admin Dashboard

`/studio/admin` (linked from `/creator`, the Creator Studio page) is the artist overview: over a
row of summary cards it shows three tables — **Submitted paintings**, **Tester sessions**
and **Beta feedback testers**. It renders the first payload inline and then polls
`/api/admin/overview` every 30 seconds (refresh interval lives in `admin_dashboard.REFRESH_MS`).

- **Submitted paintings** lists every artwork the studio knows about: the `paintings` library
  rows first, then the artworks that exist only as canvas sessions (the built-in
  `dahlia-mandala`, and an upload with no painting row), each with its session count and
  average completion. `main.canvas_artwork_names` links a canvas artwork key to its studio
  painting — one compiled upload is stored twice, because the job id is the canvas artwork
  key while the painting row written from that job has the job's package in `package_dir` —
  so one submitted image shows as one row with its sessions folded in.
- **Tester sessions** is one row per saved session, named by the artwork it paints, with a
  **Reset progress** button. It POSTs `/api/canvas/progress/<session>/reset` with
  `{"artworkKey": …}` (leave the key out to clear every artwork of that session), which
  empties `completed_json` and moves `updated_at` to now — a canvas still holding its own
  copy of the progress adopts the cleared one on its next load, which is what makes the
  reset stick. `saves` is deliberately left alone: it records engagement, not what is painted.
- `admin_dashboard.build_admin_overview(sessions, feedback, paintings, artwork_keys,
  artwork_titles)` builds the payload; everything after `sessions` is optional, so a bare
  session/feedback summary still works. The JSON endpoint is queryable directly for scripts.

**Quirk — the dashboard page is a Python string.** `admin_dashboard._PAGE` is one
triple-quoted literal, so a `\n` inside the page's JavaScript becomes a real newline in the
served file and breaks the whole inline script with `SyntaxError: Unexpected EOF` (the page
then renders its headings but no rows, because nothing after the payload is parsed). Write
`\\n` in the Python source, or keep JS strings on one line.

## Color-by-Number Design Library

`/beta/color-by-number` offers the studio's designs from the repo-root `designs/`
folder. `main.design_images()` keys each image (`png/jpg/jpeg/webp`) by its filename
stem; `GET /api/designs` lists them, `GET /designs/<file>` serves the image, and
`POST /api/designs/<slug>/compile` compiles a design on first use — reusing the upload
pipeline with `BETA_SETTINGS` — then caches the job id in `.data/design_cache.json`
so later painters open it instantly. `POST /api/designs` (multipart `file`) saves an
uploaded image into the folder; that is what the demo's **Add to Design Library**
button calls. The demo always also offers the inline built-in *Dahlia Mandala*, whose
chip clears the stored artwork key and reloads to return to it. `DELETE /api/designs/<slug>`
removes a design image from the folder and drops its cached compile with `main.purge_job`
(so a re-added file recompiles, and nothing is left behind) — the demo's per-design **×**
control calls it; the inline built-in has none.

A design opened once passes through `start_job`/`run_job`, so one design lives in three
places: the image in `designs/`, the package under `.data/jobs/<job_id>/` and the runtime DB
rows. `main.purge_job` is the one place that removes all three — the job directory, the
`jobs` row and the `paintings` row matched by `package_dir` — and deleting a design calls it.
`POST /jobs/<job_id>/cancel` only sets `cancelRequested` while the job is queued or running
(`run_job` deletes the abandoned package itself when it notices, so a cancelled job keeps no
half-built files); once the job has stopped, the same endpoint purges it instead and answers
`{"status": "removed"}`. Cleanup leaves `canvas_sessions` alone on purpose: tester progress is
test data, not a leftover file.

## Compiled Page Look (Color-by-Number)

`painting_pipeline.BETA_SETTINGS` is what the demo compiles with — uploads and
`POST /api/designs/<slug>/compile`. It asks for a grown-up coloring-book page:
`design_style: "coloring_book"` (the premium line-art pipeline, which keeps the source's
ink as ink and curates the palette), `colors: 20`, `target_regions: 360`,
`min_region_area: 28`, `outline_width: 0.28`, `simplify_tolerance: 0.24`. Photographs
still compile: version 12's recovery attempts fall back to the full-canvas photo pass, so
a photo upload lands at 360 regions / 20 colors instead of failing validation (checked
against `samples/covers/*.jpg`). Artwork compiled before this change keeps its previous
look — a package is cached by job id, and design compilation additionally in
`.data/design_cache.json` — so re-upload (or clear that cache) to see the new settings.
`renderCompiled` draws the page's printed lines at `unit*.0016` instead of `unit*.0022`,
and `.board` carries `--ink:#1b1b1b` for the line colour.

The board itself is kept on screen by the `.stage` rule: it is `position:sticky` with
`align-self:start` and `align-items:flex-start`. The right-hand `.panel` is much taller than
the board (all nine media, the palette, the toolbar), and it stretches the grid row — with the
old `align-items:center` the stage grew to the panel's height and centred the board in it, so
on a normal desktop viewport the whole colouring page landed below the fold (board top ≈1086px
in a 720px viewport). `align-items:flex-start` puts the board at the top of the stage, and the
sticky stage keeps it in view while the panel scrolls. The `max-width:780px` block resets the
stage to `position:static` for the single-column mobile layout.

## Testing

```sh
docker compose -f docker-compose.base44.yml exec web python -m pytest
```
