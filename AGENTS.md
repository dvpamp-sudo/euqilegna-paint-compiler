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

## Behavior note: the illustration region cap is a hard failure

The cap check is a deliberate "fail early" guard: when the illustration pipeline
produces more regions than `max(target_regions*1.35, target_regions+60)`, the
attempt raises instead of exporting an unusable package. Most premium samples
compile fine (`afrofuturist-stargazer` → 612 regions under its 648 cap), but
heavily detailed ones can over-segment and fail (`midnight-jazz` → ~2100–2300
regions vs its 648–756 cap). This is pre-existing: the un-corrupted `2cba359`
version fails the same artwork through `validate_package`'s region-count check,
so it is not a regression from the indentation fix.

## Secondary service (not in preview)

`platform_api/app:app` is a separate modular compiler platform (port 8100 in
the original `.bat` scripts). It is not part of the preview setup; the main
`main:app` on port 3000 is the primary studio.
