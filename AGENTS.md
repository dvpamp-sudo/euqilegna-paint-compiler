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

## Key Architecture

- `main.py` — FastAPI app, routes, job orchestration (single ThreadPoolExecutor worker)
- `compiler_core.py` — core compilation pipeline (region detection, palette assignment, SVG vectorization)
- `compiler.py` — thin wrapper that calls `compile_artwork` from `compiler_core`
- `testing_runtime_v10.py` — SQLite-backed beta testing runtime, feedback, data dir resolution
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

## Testing

```sh
docker compose -f docker-compose.base44.yml exec web python -m pytest
```
