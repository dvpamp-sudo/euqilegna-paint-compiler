# Deploy Version 10 — Permanent Testing Release

## What this release adds

- Intelligent artwork profiles and compiler recovery
- Version 9 pointer/touch/Apple Pencil engine
- One compiler worker to prevent simultaneous memory spikes
- SQLite-backed persistent job and feedback records
- Render persistent-disk support
- Service-restart detection for interrupted jobs
- Family & Friends Beta portal
- Mobile/iPad feedback form
- Beta results dashboard and CSV export

## Replace and verify locally

Copy the files into your GitHub repository:

`C:\Users\dvpam\OneDrive\Documents\GitHub\euqilegna-paint-compiler`

Run:

`VERIFY_VERSION_10.bat`

Start locally:

`C:\PythonEnvs\euqilegna\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000`

Open:

- Studio: `http://127.0.0.1:8000`
- Beta portal: `http://127.0.0.1:8000/beta-test`
- Beta results: `http://127.0.0.1:8000/beta/admin-v10`
- Runtime status: `http://127.0.0.1:8000/api/runtime-status`

## Push to Render

Commit and push:

`Implement Version 10 permanent testing release`

Render will deploy automatically.

## Add a persistent disk in Render

Your Standard service needs a disk so feedback and generated packages survive
service restarts and redeployments.

In Render:

1. Open the web service.
2. Select **Disk**.
3. Add a disk.
4. Mount path: `/var/data`
5. Size: 10 GB to begin.
6. Open **Environment**.
7. Add:
   - `EUQILEGNA_DATA_DIR=/var/data/euqilegna`
   - `EUQILEGNA_BETA_CODE=<a private code you choose>`
8. Redeploy.

Without a persistent disk, the application still runs, but files can disappear
during a redeploy.

## Share with testers

After deployment, share:

`https://YOUR-RENDER-URL/beta-test`

The private access code appears on that page and in:

`https://YOUR-RENDER-URL/beta/admin-v10`

Testers need only Safari, Chrome, or another browser. They do not need Python,
VS Code, Base44, or any other app.
