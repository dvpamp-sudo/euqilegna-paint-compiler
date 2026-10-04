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

1. **Seed budget before watershed** — `seed_ceiling = max(60, max(target_regions*1.35, target_regions+60))`
   is passed to `build_markers(..., max_seeds=seed_ceiling)`. `build_markers` is
   now a thin wrapper over `_build_markers_at_threshold`: it builds seeds once,
   and if the count exceeds `max_seeds` it raises `seed_min_area` (initial scale
   `max(2.0, count/max_seeds)`, ×1.7 per retry, up to 4 retries) so microscopic
   colour specks are absorbed by their neighbours instead of becoming regions of
   their own. `max_seeds=None` (the default) reproduces the old behaviour
   exactly, so other callers are unaffected. A seed count below 2 still falls
   back to the full-canvas Photo pipeline as before.

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
`midnight-jazz` reached 524 regions (previously ~2100–2300 against its 648–756
cap) and `afrofuturist-stargazer` compiles at 612 under its 648 cap. The
`validate_package` region-count check and the fail-early guard are untouched.

When tuning this for a new artwork, adjust in the order above — a wider seed
budget (stage 1) is much cheaper than more merge attempts (stage 3), and
loosening `color_tolerance` in stage 3 is what changes the look of the result
most, since it accepts less similar neighbours.

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

## Canvas gestures on the paint page

One pointer = tap paints, drag pans (only when zoomed past 100%). A drag has to
travel `PAN_THRESHOLD` (12px) before it counts as a pan, so the hand-shake of an
ordinary click can never swallow the paint or shift the artwork. Two pointers =
pinch to zoom, anchored on the midpoint between the fingers so the spot being
studied stays put; `touch-action:none` on `#artboard` is what lets the browser
hand these to us. `finishPinch()` arms a short `pinchGuard` that swallows the
stray click a lifting finger can emit, which would otherwise paint a region.

## Painting flow: the section you click sets the color

`handleRegion` adopts the clicked section's own color, so one click fills a
numbered region without selecting that color in the palette first (the old
"finish color X before moving to color Y" warning is gone). After each fill the
selection **stays on that color and moves on to the next section of it**:
`markNextSection(colorId)` — shared with the Hint button — rotates on from the
section just painted and drops the pink marker on the next unpainted section
whose number is usable, so the next tap lands where the painting was heading.
Only when the whole color is finished does `nextColorAfter` hand the selection
over to the next unfinished color. The pan/zoom that used to sit inside the Hint
button now lives in `focusMarkedSection()`, which only the button calls.

## Painting progress is saved per artwork and resumes where you stopped

The player saves silently after every fill (`save(false)` in `handleRegion` and
`paintRegionProgrammatically`) into `localStorage` under
`euqilegna-paint-event-` + the artwork's path, so progress belongs to that one
artwork. On load it restores the whole working position, not just the fills:
`selectedColor` (only if that color still exists in `PALETTE`), `skippedColors`,
and `hintedRegionId`. The saved id is the section last painted — the marker's
rotation anchor — so startup calls `markNextSection(selectedColor)` to put the
pink marker back on the next unpainted section of the color you were filling,
falling back to `showHintMarker` when the saved id is an unpainted section a
Hint had marked. A saved id or color that no longer exists (an artwork
recompiled since) is ignored, so the page still opens clean.

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
