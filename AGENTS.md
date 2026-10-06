# AGENTS.md — Base44 dev environment notes

## App overview

Euqilegna Interactive Digital Art Studio — a FastAPI (uvicorn) Python app that
serves an HTML paint-by-number / digital art studio. Single process, no
database service; uses SQLite (via `testing_runtime_v10.py`) for persistent job
and beta-feedback state.

- Entry point: `main:app`
- Default run: `python -m uvicorn main:app --host 0.0.0.0 --port 3000 --reload`
- Health check: `GET /health` → JSON `{"ok": true, ...}`

## Running here

`docker compose -f docker-compose.base44.yml up -d --build`

The Base44 dev-environment files (`docker-compose.base44.yml`,
`Dockerfile.base44`, `.base44/environment.json`) are **sandbox-local and
untracked** — excluded through `.git/info/exclude` so they never show up in the
repo or in pull requests. They exist on disk in the sandbox (the preview needs
them); re-add one with `git add -f <path>` if it should be versioned.

- Image: `python:3.12-slim` + system libs (`libgl1`, `libglib2.0-0`, `libgomp1`)
  for opencv/scikit-image. Deps installed at build time from `requirements.txt`.
- Source is bind-mounted at `/app`; uvicorn `--reload` picks up edits live.
- Data persisted in the `euqilegna_data` Docker volume at `/var/data/euqilegna`
  (via `EUQILEGNA_DATA_DIR` env var).
- Port 3000 is the user-facing web entry point.

## No external secrets required

The app boots without any external credentials. `EUQILEGNA_BETA_CODE` is
auto-generated if absent. `EUQILEGNA_DATA_DIR` is set in compose.

## Known quirk: compiler_core.py indentation

The imported commit `7eba90b` ("Fail early when illustration region cap is
exceeded") mangled `compiler_core.py`: it added a fail-early cap check **and**
stripped indentation from the tail of `compile_artwork` (from `region_ids = [...]`
to the end of the function), causing `SyntaxError: 'return' outside function`.
A naive "add 4 spaces everywhere" re-indent is WRONG — the loss was not uniform
(the outer block lost 4 spaces but the for-loop body ended up over-indented),
which silently moved the Pixel Coverage Guarantee check *inside* the vectorize
loop and made every compile fail coverage.

Correct fix: restore from the last good commit and re-apply only the intended
change:

    git show 2cba359:compiler_core.py > compiler_core.py   # last correct version

then re-add the cap check with its variable defined (the upstream check
referenced `illustration_hard_cap`, which was only computed in the line-art
recovery path, so it raised `UnboundLocalError` for the illustration pipeline):

    illustration_hard_cap = max(int(target_regions * 1.35), target_regions + 60)
    if active_pipeline == "illustration" and len(region_ids) > illustration_hard_cap:
        raise ValueError(...)

Verify with `diff -w -B` against `2cba359` — only that block should differ.

`2cba359` also added `"illustrationRegionCount": illustration_region_count` to the
`update(64, "Closed paint regions created")` call in the **line-art** branch, but
that variable was only ever assigned on the nested illustration-recovery path.
Every other line-art compile — the normal `marker_count >= 3` case and the sparse
`marker_count < 2` photo fallback — died with
`UnboundLocalError: cannot access local variable 'illustration_region_count'`,
which the V12 orchestrator reports as "cannot access local variable ...". The
variable is now defaulted to `marker_count` before the `if marker_count < 3:`
block and refreshed in the photo fallback, so the progress report is always
defined.

## Behavior note: the illustration region cap is a hard failure

The cap check is still a deliberate "fail early" guard: when the illustration
pipeline produces more regions than `max(target_regions*1.35, target_regions+60)`,
the attempt raises instead of exporting an unusable package. It now only fires if
all three budget layers below fail to bring the count down.

## Illustration region budget is enforced in three stages

Region count was previously controlled only *after* watershed, by a merge that
folds away regions which are already small and near-identical. That is enough for
gentle images but stalls on very detailed sources, where thousands of similarly
sized regions come out of watershed and no cheap post-merge brings them back
down. The pipeline now works on the problem in three layers, in order
(`compile_artwork`, illustration branch):

1. **Seed budget before watershed** — `seed_ceiling = max(60, target_regions)`
   is passed to `build_markers(..., max_seeds=seed_ceiling)`. Every seed becomes a
   region and heavily inked art barely merges afterwards (the ink-boundary guard
   blocks it), so the ceiling is the target itself: seeding at the hard cap left
   no headroom (877 seeds -> 895 regions after island splitting -> fail-early
   guard). `build_markers` labels every colour's interior components **once**
   into a `_SeedTable` (area + mean detail per component) and, when the count
   exceeds `max_seeds`, finds the *smallest* `seed_min_area` that fits by
   doubling then bisecting (the count only falls as the threshold rises), then
   places seeds once. `max_seeds=None` reproduces the original behaviour
   bit-for-bit (verified against the old per-component loop on corner, middle and
   edge crops, and the "nothing qualifies" fallback). A seed count below 2 still
   falls back to the full-canvas Photo pipeline as before.

   Two earlier bugs lived here. (a) The old seeding tested `components == comp`
   against the whole frame for each component — quadratic — so a detailed 1800px
   source (57k components) spent ~15 minutes in "Building closed-region
   markers" *per attempt* (up to 5 calls with the retry loop); it is now ~2 s.
   (b) The retry loop multiplied `seed_min_area` by `count/max_seeds` and then by
   1.7 each retry, overshooting from 12,664 seeds to **74** (ceiling 877), which
   produced a 68-region artwork with fidelity 0.552.

2. **First merge** — `adaptive_merge_regions(...)`, unchanged in the default
   case. It gained two opt-in keyword args, both `None` by default: `size_ceiling`
   replaces the adaptive area limit (`min_region_area * mode_area_factor * (1.75 - 1.30*detail) * pressure`)
   with a flat value, and `color_tolerance` replaces the mode/detail colour limit
   (11.0 high-detail, 20.0 relaxed, 16.0 otherwise). Omitting both keeps the
   previous heuristic bit-for-bit.

3. **`reduce_regions_to_budget(...)` safety net** — runs straight after the first
   merge, targeting `max(8, int(paint_budget * 0.85))` with `max_attempts=3`
   (0.85 deliberately leaves headroom for the paintability repair that follows).
   Each attempt grows the merge pressure: the size ceiling becomes the area at
   the index implied by the region excess (`round(excess * (1.0 + 0.4*attempt))`),
   and colour tolerance becomes `base + 12.0 * attempt` (`base` = 20.0 relaxed,
   16.0 otherwise). It stops as soon as the count hits the target or a pass makes
   no progress. The ink-boundary guard inside `adaptive_merge_regions` is never
   relaxed, so original line work stays protected.

Net effect: detailed samples that used to over-segment and abort now compile —
`midnight-jazz` compiles at 697 regions (cap 756; it was ~2100–2300 before the
budget existed) scoring 0.751, and `afrofuturist-stargazer` at 520 (cap 648)
scoring 0.737. The `validate_package` region-count check and the fail-early
guard are untouched.

When tuning this for a new artwork, adjust in the order above — a wider seed
budget (stage 1) is much cheaper than more merge attempts (stage 3), and
loosening `color_tolerance` in stage 3 is what changes the look of the result
most, since it accepts less similar neighbours.

## V12 recovery is bounded by time, not attempt count

`compiler_v12/orchestrator.py` used to `break` after the second attempt whenever
no attempt had passed (`if index >= 2 and best is None`). On a line-art source
the first two strategies (`smart-auto`, `full-canvas-illustration-recovery`) can
both land just under the 0.68 fidelity gate — e.g. 0.585 and 0.672 — while the
third, `full-canvas-photo-recovery`, passes (the photo pipeline's tonal
similarity is much higher on these images). The early break threw away that
viable package and the user saw "Version 12 could not produce a package that
passed validation".

The guard is now `time.monotonic() - recovery_started_at > RECOVERY_TIME_BUDGET_SECONDS`
(900s), so every planned strategy gets a chance on a fast image while a slow one
still can't run for hours. Note the budget is checked *between* attempts: a first
attempt that alone exceeds it (the marker slowness above did) means no fallback
ever runs.

## Hatched / scribbled line art cannot reach the 0.68 fidelity gate

`validate_package` scores `0.72*tonal + 0.28*edgeIoU` of the flat-fill
`preview.png` against the source, pixel by pixel. A dense hand-hatched
illustration (e.g. the "Afro chibi boy" upload: thousands of pen strokes on
white) loses structurally on both terms — a flat region covering 50% ink/50%
paper is ~127 off per pixel, and the preview's edges never line up with the
hatching (edgeIoU ~0.07). Measured with ~500-670 regions: `smart-auto` 0.622,
`full-canvas-illustration-recovery` 0.614, `full-canvas-photo-recovery` 0.641 —
all structurally valid (region cap, tiny-region ratio, required files), all
below 0.68, and more regions barely help.

So when **no** strategy passes, the orchestrator falls back to the best attempt
whose *only* error is fidelity (`validate_package` reports `fidelityOnly`) **and**
whose score is at least `ACCEPTABLE_SIMILARITY_FLOOR = 0.60`, instead of failing
the upload. That package is delivered with `metadata.v12ValidationPassed = false`,
`v12FidelityBelowThreshold = true`, and a final progress message saying it is the
closest match. Anything under 0.60, or with any other validation error, still
fails. A strategy that passes 0.68 always wins over the fallback, so curated
samples are unaffected. All three strategies run before the fallback picks, so a
hatched upload takes ~15-18 minutes (each attempt ~5-7 min at ~500-670 regions:
`adaptive_merge_regions`, `repair_region_coverage` and the paintability pass are
all O(regions x pixels)).

## Player template changes need a recompile to show up

The interactive paint page is not rendered per request: `compiler_core.py` bakes
`interactive_player.html` into the artwork's package at compile time, and
`/premium/{sample}/play` just serves that file. It lands in the container's own
filesystem (not the repo, not the volume): `/root/AppData/Local/EuqilegnaDigitalArtStudio/generated/premium/<sample>/package/`.

So a template edit (e.g. the canvas gesture code) only appears on already
compiled artworks after regenerating them — `/api/premium/{sample}/compile`
short-circuits when the player file exists, so delete the package dir first:

    docker compose -f docker-compose.base44.yml exec -T web \
      sh -c 'rm -rf /root/AppData/Local/EuqilegnaDigitalArtStudio/generated/premium/<sample>'
    curl -s -X POST http://localhost:3000/api/premium/<sample>/compile

Then poll `/runtime/jobs/<jobId>` until `status` is `complete` (~4–5 min for
`afrofuturist-stargazer`). Artworks compiled by an earlier session keep serving
the old template until this is done.

When the template edit is confined to the player script (as most are), a full
recompile is unnecessary: every package already stores the exact inputs
`build_interactive_player` needs, so the player alone can be regenerated in
place in seconds —

```python
from compiler_core import build_interactive_player
build_interactive_player(
    pkg,
    json.loads((pkg / "regions.json").read_text()),
    json.loads((pkg / "palette.json").read_text()),
    (pkg / "paintMap.svg").read_text(),            # blank_svg
    (pkg / "paintMap_reference.svg").read_text(),  # reference_svg
)
```

This produced a **byte-identical** file to a real full recompile for the
already-fixed `2408e644...` package (`regions.json` records carry the same
`label`/`path`/`paintability` object the compiler passes in). Because region ids
are preserved, players' saved paint progress stays valid — a full recompile can
land on a different V12 attempt and renumber regions instead. Refresh the
package ZIP afterwards (flat `zipfile` over `outputDir/*`, as `run_job` does), or
`/jobs/{id}/download` and the premium download still hand out a stale player.

## Canvas gestures on the paint page

One pointer = tap paints, drag pans (only when zoomed past 100%). A drag has to
travel `PAN_THRESHOLD` (12px) before it counts as a pan, so the hand-shake of an
ordinary click can never swallow the paint or shift the artwork. Two pointers =
pinch to zoom, anchored on the midpoint between the fingers so the spot being
studied stays put; `touch-action:none` on `#artboard` is what lets the browser
hand these to us. `finishPinch()` arms a short `pinchGuard` that swallows the
stray click a lifting finger can emit, which would otherwise paint a region.

## Canvas zoom stays a plain 2D transform

`#canvasContent` is zoomed with `translate(...) scale(...)` and must not carry
`will-change:transform`, `backface-visibility:hidden`, or a `translate3d()`
transform. Any of those promote the canvas to its own composited layer, and
Chrome then magnifies a rasterized bitmap of that layer instead of re-rendering
the vectors, so the small region numbers look smeared once the customer zooms in
(a 400% zoom needed a 4000px-wide layer raster, which the browser caps). With no
promotion the SVG is re-rendered as vectors at the zoomed scale and the digits
stay crisp at every zoom level. Note the artwork's `.paint-region` outlines use
`vector-effect:non-scaling-stroke`, so they stay one screen pixel wide at any
zoom — only the labels are affected by this quirk.

This is a player-only fix, so it reaches already-compiled artworks through the
in-place player regeneration above; no recompile is needed. Verified on
`noir-vinyl` (846 regions): at 400% the canvas reports `will-change:auto` and a
2D `matrix`, all 837 unpainted numbers render, and a drag still pans.

## Painting flow: the section you click sets the color

`handleRegion` adopts the clicked section's own color, so one click fills a
numbered region without selecting that color in the palette first (the old
"finish color X before moving to color Y" warning is gone). After each fill the
selection **stays on that color**: the painted section's id is kept as the
rotation anchor, and no hint is drawn — `markNextSection(colorId)`, shared with
the Hint button, runs **only** when the reader presses **Show One Hint**, where
it rotates on from that anchor and drops the pink marker on the next unpainted
section whose number is usable. Hints therefore stay hidden unless requested.
Only when the whole color is finished does `nextColorAfter` hand the selection
over to the next unfinished color. The pan/zoom that used to sit inside the Hint
button now lives in `focusMarkedSection()`, which only the button calls.

## Painting progress is saved per artwork and resumes where you stopped

The player saves silently after every fill (`save(false)` in `handleRegion` and
`paintRegionProgrammatically`) into `localStorage` under
`euqilegna-paint-event-` + the artwork's path, so progress belongs to that one
artwork. On load it restores the whole working position, not just the fills:
`selectedColor` (only if that color still exists in `PALETTE`), `skippedColors`,
and `hintedRegionId`. A restored anchor never reopens a hint: startup clears
`hintedRegionId` before the first repaint, so a reload shows no marker and no
glow, and the next **Show One Hint** starts from the color's first unpainted
section. A saved id or color that no longer exists (an artwork recompiled
since) is ignored, so the page still opens clean.

Like any template change, this only reaches an already-compiled artwork after
that artwork is recompiled (see above).

## Painting never moves the artwork

Two guards keep a tap from shifting the canvas:

- `lockCanvasView()` is taken at the top of `handleRegion` and restored at the
  end, so a paint leaves `zoom`/`panX`/`panY` exactly as they were, whatever the
  input path changed on the way in.
- A pointer released **off** the canvas used to stay in `activePointers`, which
  made the next single tap look like a second finger and be read as a pinch
  (`startPinch`), panning and zooming the artwork while painting.
  `forgetReleasedPointer` is bound to window `pointerup`/`pointercancel` (bubble
  phase, so it runs after the canvas handlers and leaves ordinary taps and drags
  untouched), and window `blur` clears the set.

## Customers can compile their own uploads into a playable artwork

`/studio/upload` (`studio/upload.html`) is the "bring your own image" interface.
It posts the chosen file straight to the existing `POST /jobs` endpoint — the
same compiler path `/create` uses — with premium-grade settings (`preset` from
the difficulty card, `design_style=smart_auto`, `outline_width=0.24`,
`simplify_tolerance=0.18`), polls `GET /jobs/{id}` for progress, and on
completion links to the playable result.

Unlike `/create`, which ends in a ZIP download, this flow opens the baked
`interactive_player.html` for the job:

    GET /my-artwork/{job_id}/play   →  outputDir/interactive_player.html

`outputDir` lives under `RUNTIME_DATA_DIR/jobs/<id>/package`, i.e. the
`euqilegna_data` volume (`/var/data/euqilegna`), so customer uploads survive a
container restart. The page keeps a local record of compiled uploads in
`localStorage['euqilegna-my-artwork-v1']` (jobId + downscaled thumbnail) and
renders them in its "Your images" grid; there is no server-side upload index.
Each record also carries `style` (from the job's `metadata.artworkProfile`,
mapped to Portrait / Line art / Illustration / Landscape / Photo / Graphic, else
title-cased), `difficulty` from `metadata.regions` (<=400 Easy, <=650 Medium,
else Detailed, 0 Unrated) and free-form `labels` the reader types.

### "Your images" filters

Three chip rows sit above the grid — Style, Difficulty, My labels — each with an
`All` chip and per-category counts; the three selections combine with AND and the
grid shows a "Nothing in this category" panel when a combination is empty. The
rows are rebuilt from the records on every render, so a category disappears when
its last artwork does.

Labels are edited in place: `＋ label` on a card swaps the tag row for an inline
`input.tag-input`, committing on Enter or blur (Escape cancels) into that
record's `labels`. Clicking a card's label tag removes just that label and drops
the active label filter back to `All`. Labels are deliberately *not* a
comma-free format — they are typed comma-separated in that one field.

`saveRecord(jobId, job)` needs the job payload to derive style/difficulty;
`render(job)` passes it. A record written before the filters existed has neither
field and shows as `Other` / `Unrated` until backfilled (fetch `GET /jobs/{id}`
for each stored jobId and re-save with `styleOf`/`difficultyOf`).

Entry points to the page: the premium library hero CTA (`/samples`), the studio
nav (`/`), and the premium launcher panel (`/samples/{sample}`).

## `GET /jobs/{job_id}/download` had the same indentation mangling

`test_job`, `qa_job` and `download_job` all carried a dead
`if job["status"] ...` block nested after `raise HTTPException(404)`, leaving
`player_path`/`qa_path`/`zip_path` unbound and every call a 500
(`UnboundLocalError`). All three are now fixed: `download_job` (the upload
interface links to it) plus `test_job` and `qa_job`, whose status checks were
de-nested so `player_path`/`qa_path` are always defined.

## Secondary service (not in preview)

`platform_api/app:app` is a separate modular compiler platform (port 8100 in
the original `.bat` scripts). It is not part of the preview setup; the main
`main:app` on port 3000 is the primary studio.

## Finer sections come from the region targets — never from the minimum area

Section fineness is driven by `target_regions`, which is clamped in four places
and all of them had to move together: the profile ceilings in
`artwork_profiles.py`, the per-pipeline clamps in `compiler_core.compile_artwork`
(photo 520 -> 900, graphic_monochrome 1100 -> 1250, default 650 -> 900), the V12
recovery caps in `compiler_v12/recovery.py` (illustration/graphic recovery
480 -> 760, photo 650 -> 950), and the user-facing ladders in `main.py`,
`studio/upload.html`, `premium_catalog.py` and `platform_ui/index.html`
(360/500/650/900 -> 480/700/950/1400 for the create and upload presets).
Verified on `afrofuturist-stargazer`, which previously compiled at 520 regions:
the illustration path now yields 613 and the photo path 900, both passing
`validate_package`.

The catalogue's 950-1000 targets were silently landing at 900 (photo paths) and
760 (recovery attempts), so the ceilings above moved again to **1000**: the two
photo-pipeline clamps in `compile_artwork` (the `active_pipeline == "photo"`
branch and the low-coverage photo fallback), the three V12 recovery caps
(illustration/graphic 760 -> 1000, photo 950 -> 1000) and the `photo` profile's
`target_region_max`. The illustration branch itself needed no change — its
profile ceilings (portrait 1150, decorative 1250, graphic_monochrome 1000) were
already above the catalogue targets. All eight premium artworks were then
recompiled from a deleted package dir (the compile endpoint short-circuits while
`interactive_player.html` exists) and each finished in 6-9 minutes:

| sample | regions | target | pipeline | validation |
|---|---|---|---|---|
| afrofuturist-stargazer | 950 | 950 | photo | passed 0.742 |
| midnight-jazz | 1082 | 900 | illustration | passed 0.758 |
| noir-vinyl | 916 | 820 | illustration | passed 0.725 |
| golden-koi | 900 | 900 | photo | passed 0.726 |
| rainy-cafe | 1000 | 1000 | photo | passed 0.716 |
| botanical-portrait | 994 | 950 | illustration | passed 0.758 |
| art-deco-peacock | 1250 | 1250 | photo | passed 0.682 |
| brownstone-jazz | 1000 | 1000 | photo | passed 0.708 |

`art-deco-peacock` first failed the gate at 1000 regions (0.676, delivered by the
0.60 fidelity fallback) and now passes at 1250. Each rebuild renumbers regions, so
any paint progress saved on the three earlier packages is dropped; the ZIPs and
`interactive_player.html` are regenerated by `run_job` with the packages.

## Fidelity is decided by region geometry, not by palette size

`validate_package` scores `preview_reference.png` — the flat fill built from each
region's **mean colour in `processed`** (`assign_region_palette` ->
`original_region_means`), never from the paint palette. So `color_count` cannot
move the gate: peacock compiled with 24 and with 28 colours scored *identically*
(0.67634) byte for byte. The score is `0.72*tonal + 0.28*edgeIoU` against the
source file, so the levers are region size and region boundary placement.

Measured offline on peacock (`photo_slic_regions` on the same preprocessed image,
scored with `_image_similarity`) — a free way to test a hypothesis without a
10-minute build:

| target regions | score | tonal | edgeIoU | MAE |
|---|---|---|---|---|
| 1000 (matched the real build exactly) | 0.67634 | 0.88297 | 0.14499 | 29.842 |
| 1250 | 0.68248 | 0.88729 | 0.15584 | 28.741 |

The three V12 attempts at 1000 all landed within 0.015 of each other
(`smart-auto` 0.662, illustration-recovery 0.662, photo-recovery 0.676), so the
shortfall was the region budget, not the pipeline choice. Raising peacock's
`target_regions` to 1250 (`premium_catalog.py`, inside the decorative profile's
1250 ceiling) needed the ceiling raised everywhere the request passes: the two
photo-pipeline clamps in `compile_artwork` (1000 -> 1250) and the V12 photo
recovery cap in `recovery.py` (1000 -> 1250). The rebuild then reported 1250
regions, 0 tiny, `v12ValidationPassed: true`, score 0.68248.

Do **not** buy detail by lowering `minimum_region_area`. `validate_package`
rejects a package when regions under 80px ("tiny") exceed 40% of the total, and
a lower merge floor pushes that ratio up fast: the illustration pipeline at
`target_regions=760` with `min_region_area=26` scored 0.6996 — over the 0.68
gate — yet was rejected at 260/630 tiny (41.3%), while the identical target with
the original floor of 36 passed at 0.396. The profile floors (18-70), the
recovery floors (36/42) and the catalogue `min_region_area` values therefore stay
as they were. The photo pipeline is the exception: its SLIC superpixels produced
0 tiny regions at 900 regions, which is why that pipeline's own floor could drop
from 70 to 48.

Expect slower compiles at the higher targets (every pass is
O(regions x pixels)); on these samples one illustration attempt still finishes
inside the 900s V12 recovery budget.
