# Version 12.5 — Persistent Render Job State

This release fixes `/jobs/<id>` returning `404 Not Found` while a customer is
waiting for a long Render compilation.

## Root cause

The web page knew the job ID, but `/jobs/<id>` looked only in a Python in-memory
dictionary. If Render restarted/replaced the process, the dictionary disappeared
even though the browser continued polling the same job ID.

## Version 12.5

- saves newly uploaded jobs to SQLite immediately
- persists full internal job state, including generated-package paths
- `/jobs/<id>` falls back to SQLite when memory does not contain the job
- test/QA/download routes hydrate persisted jobs too
- cancellation state is persisted
- service restart converts unfinished jobs to `interrupted` instead of returning 404
- includes `/runtime/jobs/<id>` diagnostic endpoint

## Render persistent disk

`render.yaml` already configures:

- `EUQILEGNA_DATA_DIR=/var/data/euqilegna`
- a 10 GB persistent disk mounted at `/var/data`

Keep those settings enabled in Render. Without a persistent disk, SQLite cannot
survive a full service replacement.

## Expected behavior after a restart

Instead of:

`404 Compilation job not found`

the existing job endpoint returns the persisted job, normally with status:

`interrupted`

The UI can then offer Retry without losing the job record.
