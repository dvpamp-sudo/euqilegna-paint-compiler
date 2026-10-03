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

`compiler_core.py` (a ~4500-line generated file) had a block (lines ~4097–4488
inside `compile_artwork`) that lost its 4-space indentation, causing a
`SyntaxError: 'return' outside function`. This was fixed by prepending 4 spaces
to every line in that range. If the file is regenerated/overwritten, re-check
that block's indentation.

## Secondary service (not in preview)

`platform_api/app:app` is a separate modular compiler platform (port 8100 in
the original `.bat` scripts). It is not part of the preview setup; the main
`main:app` on port 3000 is the primary studio.
