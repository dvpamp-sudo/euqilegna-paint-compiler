
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
import shutil
import tempfile
import time
import zipfile
import json
import os
import csv
import io

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from compiler import compile_artwork
from painting_pipeline import BETA_SETTINGS, record_painting
from studio_config import (
    APP_NAME, APP_VERSION, PROJECT_ROOT, PREMIUM_GENERATED_ROOT,
    GALLERY_FILE, ANALYTICS_FILE, initialize_storage,
)
from premium_catalog import PREMIUM_CATALOG
from studio_storage import read_json, append_json_line
from testing_runtime_v10 import (
    RuntimeDatabase,
    feedback_csv,
    render_beta_portal,
    render_feedback_form,
    resolve_data_dir,
)


app = FastAPI(title=f"{APP_NAME} {APP_VERSION}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=1)
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = Lock()

initialize_storage()

PROJECT_ROOT = Path(__file__).parent
RUNTIME_DATA_DIR = resolve_data_dir(PROJECT_ROOT)
RUNTIME_DB = RuntimeDatabase(RUNTIME_DATA_DIR / "euqilegna_runtime.sqlite3")
BETA_ACCESS_CODE = RUNTIME_DB.ensure_default_invite()
INTERRUPTED_ON_STARTUP = RUNTIME_DB.mark_interrupted_jobs()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in job.items()
        if key not in {"workDir", "inputPath", "outputDir", "zipPath", "cancelRequested"}
    }


def get_job_record(job_id: str, *, hydrate: bool = True) -> dict[str, Any] | None:
    """Return a job from memory, falling back to persistent SQLite storage."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            return job

    persisted = RUNTIME_DB.load_job(job_id)
    if persisted is None:
        return None

    if hydrate:
        with jobs_lock:
            jobs[job_id] = persisted
    return persisted


def set_job(job_id: str, **updates: Any) -> None:
    snapshot = None
    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        job = RUNTIME_DB.load_job(job_id)
        if job is None:
            return
        with jobs_lock:
            jobs[job_id] = job

    with jobs_lock:
        jobs[job_id].update(updates)
        snapshot = dict(jobs[job_id])

    RUNTIME_DB.save_job(snapshot)


def is_cancelled(job_id: str) -> bool:
    job = get_job_record(job_id)
    return bool((job or {}).get("cancelRequested"))


def progress_update(job_id: str, percent: int, stage: str, stats: dict) -> None:
    elapsed = float(stats.get("elapsedSeconds", 0))
    eta = None
    if 1 <= percent < 100 and elapsed > 0:
        eta = max(0, round((elapsed / percent) * (100 - percent)))

    log_entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "percent": percent,
        "message": stage,
    }

    job = get_job_record(job_id)
    if not job:
        return

    with jobs_lock:
        job = jobs[job_id]
        job["percent"] = percent
        job["stage"] = stage
        job["stats"] = stats
        job["elapsedSeconds"] = round(elapsed)
        job["estimatedSecondsRemaining"] = eta
        job["updatedAt"] = utc_now()
        job.setdefault("logs", []).append(log_entry)
        job["logs"] = job["logs"][-100:]
        snapshot = dict(job)

    RUNTIME_DB.save_job(snapshot)


def run_job(job_id: str, settings: dict[str, Any]) -> None:
    with jobs_lock:
        job = jobs[job_id]
        input_path = Path(job["inputPath"])
        output_dir = Path(job["outputDir"])
        zip_path = Path(job["zipPath"])

    set_job(
        job_id,
        status="running",
        stage="Starting compiler",
        percent=1,
        startedAt=utc_now(),
        updatedAt=utc_now(),
    )

    try:
        metadata = compile_artwork(
            input_path=input_path,
            output_dir=output_dir,
            progress_callback=lambda p, s, stats: progress_update(job_id, p, s, stats),
            cancel_check=lambda: is_cancelled(job_id),
            **settings,
        )

        if is_cancelled(job_id):
            raise RuntimeError("Compilation cancelled by user.")

        progress_update(
            job_id,
            98,
            "Packaging generated files",
            {
                "finalRegions": metadata.get("regions"),
                "colors": metadata.get("colors"),
                "inkPaths": metadata.get("inkPaths"),
            },
        )

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in output_dir.iterdir():
                if item.is_file():
                    archive.write(item, item.name)

        files = [item.name for item in output_dir.iterdir() if item.is_file()]
        append_json_line(
            ANALYTICS_FILE,
            {
                "type": "compilation",
                "jobId": job_id,
                "premiumSample": job.get("premiumSample"),
                "regions": metadata.get("regions"),
                "colors": metadata.get("colors"),
                "status": "complete",
            },
        )
        record_painting(
            metadata,
            source_path=input_path,
            package_dir=output_dir,
            runtime=RUNTIME_DB,
        )

        set_job(
            job_id,
            status="complete",
            stage="Complete",
            percent=100,
            metadata=metadata,
            files=files,
            downloadReady=True,
            estimatedSecondsRemaining=0,
            completedAt=utc_now(),
            updatedAt=utc_now(),
        )

    except Exception as exc:
        cancelled = "cancelled" in str(exc).lower() or is_cancelled(job_id)
        set_job(
            job_id,
            status="cancelled" if cancelled else "failed",
            stage="Cancelled" if cancelled else "Compilation failed",
            error=str(exc),
            estimatedSecondsRemaining=None,
            completedAt=utc_now(),
            updatedAt=utc_now(),
        )



@app.get("/beta-test", response_class=HTMLResponse)
def beta_test_portal(request: Request):
    public_url = str(request.base_url).rstrip("/")
    return HTMLResponse(render_beta_portal(public_url, BETA_ACCESS_CODE))


@app.get("/beta/feedback-v10", response_class=HTMLResponse)
def beta_feedback_v10(code: str = ""):
    if not RUNTIME_DB.validate_invite(code):
        raise HTTPException(status_code=403, detail="A valid beta access code is required.")
    return HTMLResponse(render_feedback_form(code))


@app.post("/api/beta-feedback-v10")
async def save_beta_feedback_v10(request: Request):
    payload = await request.json()
    code = str(payload.pop("code", ""))
    if not RUNTIME_DB.validate_invite(code):
        raise HTTPException(status_code=403, detail="Invalid beta access code.")
    payload["submittedAt"] = utc_now()
    feedback_id = RUNTIME_DB.save_feedback(payload)
    return {"ok": True, "feedbackId": feedback_id}


@app.get("/beta/admin-v10", response_class=HTMLResponse)
def beta_admin_v10():
    rows = RUNTIME_DB.feedback_rows()
    total = len(rows)
    completed = sum(1 for row in rows if row.get("completion_status") == "Completed it")
    pointer_pass = sum(1 for row in rows if row.get("pointer_worked") == 1)
    avg_enjoyment = round(
        sum((row.get("enjoyment") or 0) for row in rows) / max(1, total),
        2,
    )
    table = "".join(
        "<tr>"
        f"<td>{row.get('submitted_at','')}</td>"
        f"<td>{row.get('tester_name','')}</td>"
        f"<td>{row.get('device','')}</td>"
        f"<td>{row.get('artwork','')}</td>"
        f"<td>{row.get('completion_status','')}</td>"
        f"<td>{'Yes' if row.get('pointer_worked') else 'No'}</td>"
        f"<td>{row.get('comments','')}</td>"
        "</tr>"
        for row in rows[:200]
    )
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Euqilegna Beta Results</title>
    <style>body{{font-family:Arial;background:#fff8f1;color:#2b190f;padding:20px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:white;border:1px solid #ead6c5;border-radius:14px;padding:16px}}table{{width:100%;border-collapse:collapse;background:white;margin-top:18px}}th,td{{padding:8px;border:1px solid #ead6c5;text-align:left}}@media(max-width:800px){{.cards{{grid-template-columns:1fr 1fr}}}}</style></head>
    <body><h1>Family & Friends Beta Results</h1>
    <p>Share: <code>/beta-test</code> — Access code: <strong>{BETA_ACCESS_CODE}</strong></p>
    <div class='cards'>
    <div class='card'><strong>Responses</strong><h2>{total}</h2></div>
    <div class='card'><strong>Completed</strong><h2>{completed}</h2></div>
    <div class='card'><strong>Pointer passed</strong><h2>{pointer_pass}/{total}</h2></div>
    <div class='card'><strong>Avg. enjoyment</strong><h2>{avg_enjoyment}/5</h2></div>
    </div>
    <p><a href='/beta/feedback-v10.csv'>Download CSV</a></p>
    <table><thead><tr><th>Date</th><th>Tester</th><th>Device</th><th>Artwork</th><th>Progress</th><th>Pointer</th><th>Comments</th></tr></thead><tbody>{table}</tbody></table>
    </body></html>""")


@app.get("/beta/feedback-v10.csv")
def beta_feedback_v10_csv():
    content = feedback_csv(RUNTIME_DB.feedback_rows())
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=euqilegna_beta_feedback_v10.csv"},
    )


@app.get("/api/runtime-status")
def runtime_status():
    return {
        "ok": True,
        "version": "10.0",
        "dataDir": str(RUNTIME_DATA_DIR),
        "persistentDataConfigured": bool(
            os.getenv("EUQILEGNA_DATA_DIR")
            or os.getenv("DATA_DIR")
            or str(RUNTIME_DATA_DIR).startswith("/var/data")
        ),
        "singleCompilerWorker": True,
        "interruptedJobsRecovered": INTERRUPTED_ON_STARTUP,
        "recentJobs": RUNTIME_DB.recent_jobs(10),
    }



BETA_FEEDBACK_FILE = Path(__file__).parent / "beta_feedback.jsonl"


@app.get("/beta", response_class=HTMLResponse)
def beta_dashboard():
    path = Path(__file__).parent / "beta" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/beta/feedback", response_class=HTMLResponse)
def beta_feedback_page():
    path = Path(__file__).parent / "beta" / "feedback.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/beta/color-by-number", response_class=HTMLResponse)
def beta_color_by_number():
    path = Path(__file__).parent / "beta" / "color-by-number.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.post("/api/beta-feedback")
async def save_beta_feedback(request: Request):
    payload = await request.json()
    payload["submittedAt"] = utc_now()
    with BETA_FEEDBACK_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return JSONResponse({"ok": True})


@app.get("/beta/admin", response_class=HTMLResponse)
def beta_admin():
    rows = []
    if BETA_FEEDBACK_FILE.exists():
        for line in BETA_FEEDBACK_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

    total = len(rows)
    avg_enjoyment = round(
        sum(float(row.get("enjoyment", 0) or 0) for row in rows) / max(1, total), 2
    )
    avg_recommend = round(
        sum(float(row.get("recommend", 0) or 0) for row in rows) / max(1, total), 2
    )
    frustration_counts = {}
    for row in rows:
        for item in row.get("frustrations", []) or []:
            frustration_counts[item] = frustration_counts.get(item, 0) + 1

    table_rows = "".join(
        f"<tr><td>{row.get('submittedAt','')}</td><td>{row.get('testerName','')}</td>"
        f"<td>{row.get('experience','')}</td><td>{row.get('enjoyment','')}</td>"
        f"<td>{row.get('recommend','')}</td><td>{row.get('changeFirst','')}</td></tr>"
        for row in reversed(rows[-100:])
    )
    frustrations = "".join(
        f"<li>{name}: {count}</li>"
        for name, count in sorted(frustration_counts.items(), key=lambda item: -item[1])
    ) or "<li>No frustrations submitted yet.</li>"

    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><title>Beta Admin</title>
    <style>body{{font-family:Arial;background:#fff8f1;color:#2b190f;padding:24px}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card{{background:#fff;border:1px solid #ead6c5;border-radius:14px;padding:18px}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:9px;border:1px solid #ead6c5;text-align:left;vertical-align:top}}@media(max-width:750px){{.cards{{grid-template-columns:1fr}}}}</style></head>
    <body><a href='/beta'>← Beta Dashboard</a><h1>Beta Feedback Dashboard</h1>
    <div class='cards'><div class='card'><strong>Total responses</strong><h2>{total}</h2></div>
    <div class='card'><strong>Average enjoyment</strong><h2>{avg_enjoyment}/5</h2></div>
    <div class='card'><strong>Average recommendation</strong><h2>{avg_recommend}/10</h2></div></div>
    <h2>Reported frustrations</h2><ul>{frustrations}</ul>
    <p><a href='/beta/feedback.csv'>Download feedback CSV</a></p>
    <h2>Responses</h2><table><thead><tr><th>Date</th><th>Tester</th><th>Experience</th><th>Enjoyment</th><th>Recommend</th><th>Change first</th></tr></thead><tbody>{table_rows}</tbody></table>
    </body></html>""")


@app.get("/beta/feedback.csv")
def beta_feedback_csv():
    rows = []
    if BETA_FEEDBACK_FILE.exists():
        for line in BETA_FEEDBACK_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

    fields = [
        "submittedAt", "testerName", "experience", "enjoyment", "difficulty",
        "regions", "imageQuality", "favorite", "recommend", "changeFirst",
        "comments", "frustrations", "screen", "userAgent"
    ]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        item = {field: row.get(field, "") for field in fields}
        if isinstance(item.get("frustrations"), list):
            item["frustrations"] = "; ".join(item["frustrations"])
        writer.writerow(item)
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=euqilegna_beta_feedback.csv"},
    )



@app.get("/", response_class=HTMLResponse)
def digital_art_studio_home():
    path = Path(__file__).parent / "studio" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/studio/create", response_class=HTMLResponse)
def studio_create_flow():
    path = Path(__file__).parent / "studio" / "create.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/studio/finishes", response_class=HTMLResponse)
def studio_finishes():
    path = Path(__file__).parent / "studio" / "finishes.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/studio/music", response_class=HTMLResponse)
def studio_music():
    path = Path(__file__).parent / "studio" / "music.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/studio/gallery", response_class=HTMLResponse)
def studio_gallery():
    path = Path(__file__).parent / "studio" / "gallery.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/studio/challenges", response_class=HTMLResponse)
def studio_challenges():
    path = Path(__file__).parent / "studio" / "challenges.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))



@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    path = PROJECT_ROOT / "studio" / "dashboard.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/creator", response_class=HTMLResponse)
def creator_page():
    path = PROJECT_ROOT / "studio" / "creator.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/dashboard")
def dashboard_data():
    gallery = read_json(GALLERY_FILE, [])
    with jobs_lock:
        active_jobs = [
            job for job in jobs.values()
            if job.get("status") in {"queued", "running"}
        ]
        recent_jobs = sorted(
            jobs.values(),
            key=lambda item: item.get("updatedAt", ""),
            reverse=True,
        )[:8]

    activity = [
        {
            "label": job.get("premiumSample")
            or Path(job.get("inputPath", "Artwork")).stem,
            "status": job.get("stage", job.get("status", "Unknown")),
        }
        for job in recent_jobs
    ]
    return {
        "galleryCount": len(gallery) if isinstance(gallery, list) else 0,
        "activeJobs": len(active_jobs),
        "premiumDesigns": len(PREMIUM_CATALOG),
        "activity": activity,
    }


@app.get("/api/catalog")
def catalog_data():
    return {"items": PREMIUM_CATALOG, "count": len(PREMIUM_CATALOG)}


@app.get("/runtime/jobs/{job_id}")
def runtime_job_diagnostic(job_id: str):
    """Diagnostic view proving a job exists in persistent storage."""
    job = RUNTIME_DB.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Persistent job not found.")
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "percent": job.get("percent"),
        "stage": job.get("stage"),
        "createdAt": job.get("createdAt"),
        "updatedAt": job.get("updatedAt"),
        "persistent": True,
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "Euqilegna Interactive Digital Art Studio 10.0 Testing Release",
        "features": [
            "real-time progress",
            "current stage",
            "elapsed time",
            "estimated time remaining",
            "live logs",
            "live statistics",
            "cancel compilation",
            "automatic download",
        ],
    }


@app.get("/create", response_class=HTMLResponse)
def home():
    return r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Euqilegna Interactive Digital Art Studio 9.0</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root{
      --accent:#9c4a22;--accent2:#d47b48;--purple:#7347a8;--bg:#fff8f1;
      --panel:#fff;--line:#ead6c5;--text:#2b190f;--muted:#765d4c;
      --good:#216e39;--soft:#fff3e8;--shadow:#00000010
    }
    *{box-sizing:border-box}
    body{font-family:Arial,sans-serif;background:var(--bg);color:var(--text);max-width:1120px;margin:28px auto;padding:0 18px}
    .card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:28px;box-shadow:0 10px 28px var(--shadow)}
    h1{font-family:Georgia,serif;margin:0 0 10px;font-size:38px}
    h2,h3{margin-top:0}
    .note{background:linear-gradient(135deg,#fff3e8,#fffaf4);border:1px solid #efcfb6;padding:15px 16px;border-radius:12px;font-size:17px;line-height:1.45}
    .section-title{margin:24px 0 10px;font-size:20px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .choice-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
    label{display:block;font-weight:700;margin-top:14px}
    input,select{width:100%;padding:11px;margin-top:6px;border:1px solid #ccb7a7;border-radius:9px;background:#fff}
    button{border:0;border-radius:10px;padding:13px 18px;font-weight:700;cursor:pointer}
    .primary{background:var(--accent);color:#fff}
    .secondary{background:#f0e7df;color:var(--text)}
    #compileBtn{margin-top:22px;background:var(--accent);color:#fff;font-size:16px}
    .gallery-link{display:inline-block;margin:22px 0 0 10px;background:var(--purple);color:#fff;text-decoration:none;border-radius:10px;padding:13px 18px;font-weight:700}
    .mode-card,.analysis-card,.preview-card,.score-card{border:1px solid var(--line);border-radius:14px;padding:16px;background:#fffaf6}
    .mode-card{cursor:pointer;transition:.2s}
    .mode-card:hover,.mode-card.active{transform:translateY(-2px);border-color:var(--accent);box-shadow:0 6px 16px #0000000d}
    .mode-card strong{display:block;font-size:16px;margin-bottom:4px}
    .mode-card small{color:var(--muted)}
    .badge{display:inline-block;background:#f7e6c7;color:#714d00;border-radius:999px;padding:4px 8px;font-size:12px;font-weight:700;margin-left:6px}
    .analysis-wrap{display:grid;grid-template-columns:220px 1fr;gap:18px;margin-top:18px}
    .preview-box{height:220px;border:1px dashed #cdb8a6;border-radius:12px;background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden}
    .preview-box img{width:100%;height:100%;object-fit:contain}
    .analysis-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
    .metric{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px}
    .metric small{display:block;color:var(--muted);margin-bottom:4px}
    .metric strong{font-size:18px}
    .score{font-size:34px;font-weight:900;color:var(--good)}
    .meter{height:10px;background:#eee4dc;border-radius:999px;overflow:hidden;margin-top:8px}
    .meter>div{height:100%;background:linear-gradient(90deg,var(--accent),#e1b85b);width:0;transition:width .4s}
    details{margin-top:18px;border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:#fffaf6}
    summary{cursor:pointer;font-weight:800}
    .hidden{display:none!important}
    #progressPanel{margin-top:24px;border-top:1px solid var(--line);padding-top:22px}
    .progress-shell{height:20px;background:#eee4dc;border-radius:999px;overflow:hidden}
    #progressBar{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .35s ease}
    .progress-head{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:9px}
    #stage{font-weight:700}
    #percent{font-size:22px;font-weight:800}
    .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}
    .stat{background:#fff8f1;border:1px solid var(--line);border-radius:10px;padding:12px}
    .stat small{display:block;color:#765d4c;margin-bottom:5px}
    .stat strong{font-size:18px}
    .actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    #cancelBtn{background:#fff;color:#a71919;border:1px solid #d9aaaa}
    #downloadBtn{background:#245d34;color:#fff;text-decoration:none;border-radius:10px;padding:13px 18px;font-weight:700}
    #testBtn{background:#7347a8;color:#fff;text-decoration:none;border-radius:10px;padding:13px 18px;font-weight:700}
    #qaBtn{background:#315f7a;color:#fff;text-decoration:none;border-radius:10px;padding:13px 18px;font-weight:700}
    #logs{background:#201b18;color:#f7ede5;border-radius:10px;padding:13px;height:190px;overflow:auto;font-family:Consolas,monospace;font-size:13px;line-height:1.5}
    .success{color:#216e39;font-weight:700}.error{color:#a71919;font-weight:700}
    @media(max-width:850px){.choice-grid{grid-template-columns:1fr 1fr}.analysis-wrap{grid-template-columns:1fr}}
    @media(max-width:720px){.grid,.stats,.analysis-grid{grid-template-columns:1fr}.progress-head{align-items:flex-start}.gallery-link{margin-left:0}}

    .app-header{display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center;margin-bottom:18px}
    .brand-lockup{display:flex;gap:14px;align-items:center}.brand-icon{font-size:48px}
    .brand-sub{color:var(--purple);font-weight:900;letter-spacing:2px;margin-top:4px}
    .inspiration{background:#fff;border:1px solid #dcb26c;border-radius:14px;padding:14px 18px;text-decoration:none;color:var(--purple);font-weight:900;box-shadow:0 6px 16px #00000010}
    .experience-visual{font-size:48px;margin:8px 0}.mode-card .hours{display:block;color:var(--purple);font-weight:900;margin-top:8px}
    .receive-box{margin-top:18px;border:1px solid var(--line);border-radius:14px;padding:16px;background:#fffaf6}
    .receive-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}.receive-item{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center}.receive-item b{display:block;margin:6px 0}.receive-item small{color:var(--muted)}
    .cta-wrap{display:grid;grid-template-columns:1fr 190px;gap:12px;margin-top:18px}#compileBtn{width:100%;font-size:24px;padding:20px;background:linear-gradient(135deg,#7a2ca5,#4d1a83);box-shadow:0 10px 20px #6f2ca033}#compileBtn small{display:block;font-size:13px;font-weight:500;margin-top:6px}.preview-first{border:1px solid #d7c4e2;background:#fff7ff;color:var(--purple);font-weight:900}
    .trust-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}.trust-item{text-align:center;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px}
    .review-hero{display:grid;grid-template-columns:130px 1fr;gap:14px;align-items:center}.review-score{font-size:52px;font-weight:900;color:var(--purple)}.review-stars{color:#e1a21d;letter-spacing:3px}.review-bars>div{display:grid;grid-template-columns:120px 1fr;gap:8px;align-items:center;margin:7px 0}.review-bars i{height:6px;background:#278247;border-radius:999px;display:block}
    @media(max-width:900px){.app-header{grid-template-columns:1fr}.receive-grid{grid-template-columns:1fr 1fr 1fr}.cta-wrap{grid-template-columns:1fr}.trust-row{grid-template-columns:1fr}.review-hero{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="card">
  <div class="app-header">
    <div class="brand-lockup"><div class="brand-icon">🎨</div><div><h1>Euqilegna Interactive Digital Art Studio 9.0</h1><div class="brand-sub">PREMIUM PAINT-BY-NUMBER EXPERIENCE</div></div></div>
    <a class="inspiration" href="/samples">👑 Inspiration Gallery<br><small>Browse finished examples →</small></a>
  </div>
  <p class="note"><strong>Turn any photo or artwork into a beautiful interactive Paint-by-Number experience.</strong><br>Paint online, uncover the original artwork as you go, download your finished masterpiece, create a showcase card, and share it proudly.</p>

  <form id="compileForm">
    <h2 class="section-title">1. Choose your artwork</h2>
    <label>Upload photo or artwork</label>
    <input id="artworkFile" type="file" name="file" accept="image/png,image/jpeg,image/webp" required>

    <section id="analysisSection" class="analysis-wrap hidden">
      <div class="preview-card">
        <h3>Artwork Preview</h3>
        <div class="preview-box"><img id="artworkPreview" alt="Selected artwork preview"></div>
      </div>
      <div class="analysis-card">
        <h3>✨ AI Artwork Review</h3><div class="review-hero"><div><div id="reviewScore" class="review-score">—</div><div class="review-stars">★★★★★</div><strong id="reviewLabel">Upload artwork</strong></div><div class="review-bars"><div><span>Image Quality</span><i id="barQuality" style="width:0"></i></div><div><span>Detail Level</span><i id="barDetail" style="width:0"></i></div><div><span>Printability</span><i id="barPrint" style="width:0"></i></div><div><span>Color Richness</span><i id="barColor" style="width:0"></i></div><div><span>Relaxation</span><i id="barRelax" style="width:0"></i></div></div></div>
        <div class="analysis-grid">
          <div class="metric"><small>Detected style</small><strong id="detectedStyle">—</strong></div>
          <div class="metric"><small>Resolution</small><strong id="resolutionMetric">—</strong></div>
          <div class="metric"><small>Paintability score</small><strong id="paintabilityMetric" class="score">—</strong><div class="meter"><div id="paintabilityBar"></div></div></div>
          <div class="metric"><small>Print quality</small><strong id="printMetric">—</strong></div>
          <div class="metric"><small>Estimated compile time</small><strong id="compileEstimate">—</strong></div>
          <div class="metric"><small>Estimated painting time</small><strong id="paintingEstimate">—</strong></div>
          <div class="metric"><small>Relaxation level</small><strong id="relaxationMetric">—</strong></div>
          <div class="metric"><small>Music pairing</small><strong>Smooth Jazz ☕</strong></div>
        </div>
        <p id="analysisAdvice" style="margin-bottom:0;color:var(--muted)"></p>
      </div>
    </section>

    <h2 class="section-title">2. Choose your experience</h2>
    <div class="choice-grid" id="difficultyCards">
      <div class="mode-card" data-preset="beginner" data-experience="relaxed" data-regions="360" data-minarea="70">
        <strong>😊 Beginner</strong><div class="experience-visual">🌸</div><small>Large, easy regions</small><span class="hours">2–3 hours</span>
      </div>
      <div class="mode-card" data-preset="balanced" data-experience="relaxed" data-regions="500" data-minarea="52">
        <strong>🙂 Relaxing</strong><div class="experience-visual">🦋</div><small>Calm and comfortable</small><span class="hours">4–6 hours</span>
      </div>
      <div class="mode-card active" data-preset="illustration" data-experience="balanced" data-regions="650" data-minarea="38">
        <strong>🎨 Detailed <span class="badge">Recommended</span></strong><div class="experience-visual">👩🏾‍🎨</div><small>Best balance of detail and ease</small><span class="hours">6–10 hours</span>
      </div>
      <div class="mode-card" data-preset="advanced" data-experience="detailed" data-regions="900" data-minarea="24">
        <strong>🏆 Masterpiece</strong><div class="experience-visual">🖼️</div><small>More regions and detail</small><span class="hours">10–20+ hours</span>
      </div>
    </div>

    <div class="grid">
      <div><label>Number of colors</label>
        <select name="colors" id="colorsSelect">
          <option value="24">24 — Simple</option>
          <option value="32">32 — Relaxing</option>
          <option value="40" selected>40 — Recommended</option>
          <option value="48">48 — Rich detail</option>
        </select>
      </div>
      <div><label>Artwork processing</label>
        <select name="design_style" id="designStyleSelect">
          <option value="smart_auto" selected>Smart Auto — recommended</option>
          <option value="premium_lineart">Premium line art / coloring-book design</option>
          <option value="artwork">Illustration / cartoon / chibi</option>
          <option value="photo">Photo / portrait / landscape</option>
        </select>
      </div>
      <div><label>Finished artwork style</label>
        <select name="finish_mode" id="finishModeSelect">
          <option value="original" selected>Original Detail — preserve uploaded artwork</option>
          <option value="acrylic">Acrylic Gallery — canvas grain and brush depth</option>
          <option value="watercolor">Watercolor Studio — paper texture and ink detail</option>
          <option value="oil">Oil Museum — glazing and canvas depth</option>
          <option value="pencil">Colored Pencil — layered line and paper tooth</option>
          <option value="pastel">Soft Pastel — softened pigment and textured paper</option>
          <option value="mixed_media">Mixed Media — watercolor pigment with selective ink</option>
        </select>
      </div>
    </div>

    <input type="hidden" name="preset" id="presetField" value="illustration">
    <input type="hidden" name="experience_mode" id="experienceField" value="balanced">
    <input type="hidden" name="target_regions" id="regionsField" value="650">
    <input type="hidden" name="min_region_area" id="minAreaField" value="38">

    <details>
      <summary>⚙ Professional Artist Controls — Advanced Fine Tuning</summary>
      <div class="grid">
        <div><label>Maximum regions</label><input type="number" id="advancedRegions" value="650"></div>
        <div><label>Minimum region area</label><input type="number" id="advancedMinArea" value="38"></div>
        <div><label>Outline width</label><input type="number" step="0.01" name="outline_width" value="0.38"></div>
        <div><label>Simplify tolerance</label><input type="number" step="0.05" name="simplify_tolerance" value="0.35"></div>
        <div><label>Auto-crop</label><select name="auto_crop"><option value="true" selected>Yes</option><option value="false">No</option></select></div>
        <div><label>Generate printable PDF</label><select name="generate_pdf"><option value="true" selected>Yes</option><option value="false">No</option></select></div>
      </div>
    </details>

    <section class="receive-box"><h3>🎁 What You’ll Receive</h3><div class="receive-grid">
      <div class="receive-item">🖌️<b>Interactive Painting</b><small>Smart reveal experience</small></div>
      <div class="receive-item">📄<b>Printable PDF</b><small>Outline and guide</small></div>
      <div class="receive-item">🎨<b>Color Guide</b><small>Complete palette chart</small></div>
      <div class="receive-item">🖼️<b>Finished Artwork</b><small>Original-quality reveal</small></div>
      <div class="receive-item">🪪<b>Showcase Card</b><small>Ready to share</small></div>
      <div class="receive-item">💬<b>Share Anywhere</b><small>Social and download</small></div>
    </div></section>
    <div class="cta-wrap"><button id="compileBtn" type="submit">✨ CREATE MY PAINT-BY-NUMBER<small id="ctaEstimate">Estimated compile time: 20–40 seconds</small></button><button id="previewFirstBtn" class="preview-first" type="button">👁 Preview First<br><small>Review your artwork</small></button></div>
  </form>
  <div class="trust-row"><div class="trust-item">🛡️ <b>Secure & Private</b><br><small>Your images stay private</small></div><div class="trust-item">💗 <b>Made for Artists</b><br><small>Calm, creative, satisfying</small></div><div class="trust-item">🏅 <b>High-Quality Results</b><br><small>Original artwork reveal</small></div></div>

  <section id="progressPanel" class="hidden">
    <div class="progress-head">
      <div>
        <div id="stage">Preparing...</div>
        <div id="statusMessage"></div>
      </div>
      <div id="percent">0%</div>
    </div>

    <div class="progress-shell"><div id="progressBar"></div></div>

    <div class="stats">
      <div class="stat"><small>Elapsed</small><strong id="elapsed">0:00</strong></div>
      <div class="stat"><small>Estimated remaining</small><strong id="eta">Calculating</strong></div>
      <div class="stat"><small>Regions</small><strong id="regions">—</strong></div>
      <div class="stat"><small>Ink paths / seeds</small><strong id="inkStats">—</strong></div>
    </div>

    <div class="stats" id="coverageStats">
      <div class="stat"><small>Pixel coverage</small><strong id="coveragePercent">Checking…</strong></div>
      <div class="stat"><small>Coverage errors</small><strong id="coverageErrors">—</strong></div>
      <div class="stat"><small>Orphan pixels</small><strong id="orphanPixels">—</strong></div>
      <div class="stat"><small>Edge repair</small><strong id="edgeRepair">Pending</strong></div>
    </div>

    <div class="actions">
      <button id="cancelBtn" type="button">Cancel Compilation</button>
      <a id="testBtn" class="hidden" href="#" target="_blank">Test Customer Experience</a>
      <a id="qaBtn" class="hidden" href="#" target="_blank">View QA Score</a>
      <a id="downloadBtn" class="hidden" href="#">Download Completed Package</a>
    </div>

    <h3>Live processing log</h3>
    <div id="logs"></div>
  </section>
</div>

<script>

const artworkFile=document.getElementById('artworkFile');
const analysisSection=document.getElementById('analysisSection');
const artworkPreview=document.getElementById('artworkPreview');
const presetField=document.getElementById('presetField');
const experienceField=document.getElementById('experienceField');
const regionsField=document.getElementById('regionsField');
const minAreaField=document.getElementById('minAreaField');
const advancedRegions=document.getElementById('advancedRegions');
const advancedMinArea=document.getElementById('advancedMinArea');
const colorsSelect=document.getElementById('colorsSelect');

function applyDifficultyCard(card){
  document.querySelectorAll('.mode-card').forEach(item=>item.classList.remove('active'));
  card.classList.add('active');
  presetField.value=card.dataset.preset;
  experienceField.value=card.dataset.experience;
  regionsField.value=card.dataset.regions;
  minAreaField.value=card.dataset.minarea;
  advancedRegions.value=card.dataset.regions;
  advancedMinArea.value=card.dataset.minarea;
  updatePaintingEstimate();
}

document.querySelectorAll('.mode-card').forEach(card=>{
  card.addEventListener('click',()=>applyDifficultyCard(card));
});

advancedRegions.addEventListener('input',()=>regionsField.value=advancedRegions.value);
advancedMinArea.addEventListener('input',()=>minAreaField.value=advancedMinArea.value);
colorsSelect.addEventListener('change',updatePaintingEstimate);

function updatePaintingEstimate(){
  const regions=Number(regionsField.value||650);
  const colors=Number(colorsSelect.value||40);
  const hours=Math.max(2,Math.round((regions/120)+(colors/18)));
  document.getElementById('paintingEstimate').textContent=`About ${hours}–${hours+2} hours`;
}

function analyzeArtwork(file){
  const url=URL.createObjectURL(file);
  const image=new Image();
  image.onload=()=>{
    artworkPreview.src=url;
    analysisSection.classList.remove('hidden');

    const width=image.naturalWidth;
    const height=image.naturalHeight;
    const pixels=width*height;
    const ratio=Math.max(width,height)/Math.max(1,Math.min(width,height));

    const name=file.name.toLowerCase();
    let style='Illustration / mixed artwork';
    if(/photo|portrait|landscape|jpg|jpeg/.test(name)) style='Photo or portrait';
    if(/line|coloring|outline|svg/.test(name)) style='Line art';
    if(/anime|chibi|cartoon|comic/.test(name)) style='Illustration / cartoon';

    let score=72;
    if(pixels>=4000000) score+=15;
    else if(pixels>=2000000) score+=10;
    else if(pixels<800000) score-=18;
    if(ratio>2.2) score-=8;
    score=Math.max(35,Math.min(98,score));

    let print='Good';
    if(Math.min(width,height)>=2400) print='Excellent ★★★★★';
    else if(Math.min(width,height)>=1600) print='Very good ★★★★☆';
    else if(Math.min(width,height)<900) print='Limited ★★☆☆☆';

    let compile='15–30 seconds';
    if(pixels>8000000) compile='45–90 seconds';
    else if(pixels>4000000) compile='30–60 seconds';

    document.getElementById('detectedStyle').textContent=style;
    document.getElementById('resolutionMetric').textContent=`${width} × ${height}`;
    document.getElementById('reviewScore').textContent=score;
    document.getElementById('reviewLabel').textContent=score>=90?'Excellent':score>=75?'Very Good':'Needs Review';
    const setReviewBar=(id,v)=>document.getElementById(id).style.width=Math.max(10,Math.min(100,v))+'%';
    setReviewBar('barQuality',score);setReviewBar('barDetail',86);setReviewBar('barPrint',minSide>=2400?95:minSide>=1600?82:55);setReviewBar('barColor',88);setReviewBar('barRelax',96);
    document.getElementById('paintabilityMetric').textContent=`${score}/100`;
    document.getElementById('paintabilityBar').style.width=score+'%';
    document.getElementById('printMetric').textContent=print;
    document.getElementById('compileEstimate').textContent=compile;
    document.getElementById('ctaEstimate').textContent='Estimated compile time: '+compile;
    document.getElementById('relaxationMetric').textContent=
      presetField.value==='advanced'?'Focused':'Calm';
    document.getElementById('analysisAdvice').textContent=
      score>=90
        ?'Excellent source image. This artwork is expected to produce a premium experience.'
        :score>=70
          ?'Good source image. Smart Auto should produce a strong result.'
          :'This image may need a higher-resolution source or simpler difficulty setting.';

    updatePaintingEstimate();
  };
  image.onerror=()=>URL.revokeObjectURL(url);
  image.src=url;
}

artworkFile.addEventListener('change',()=>{
  const file=artworkFile.files?.[0];
  if(file) analyzeArtwork(file);
});

document.getElementById('previewFirstBtn').onclick=()=>{
  if(!artworkFile.files?.length){artworkFile.click();return;}
  document.getElementById('analysisSection').scrollIntoView({behavior:'smooth',block:'center'});
};


const form = document.getElementById('compileForm');
const panel = document.getElementById('progressPanel');
const bar = document.getElementById('progressBar');
const percentEl = document.getElementById('percent');
const stageEl = document.getElementById('stage');
const statusMessage = document.getElementById('statusMessage');
const elapsedEl = document.getElementById('elapsed');
const etaEl = document.getElementById('eta');
const regionsEl = document.getElementById('regions');
const inkStatsEl = document.getElementById('inkStats');
const coveragePercentEl = document.getElementById('coveragePercent');
const coverageErrorsEl = document.getElementById('coverageErrors');
const orphanPixelsEl = document.getElementById('orphanPixels');
const edgeRepairEl = document.getElementById('edgeRepair');
const logsEl = document.getElementById('logs');
const cancelBtn = document.getElementById('cancelBtn');
const downloadBtn = document.getElementById('downloadBtn');
const testBtn = document.getElementById('testBtn');
const qaBtn = document.getElementById('qaBtn');
const compileBtn = document.getElementById('compileBtn');

let activeJobId = null;
let pollTimer = null;
let autoDownloaded = false;

function formatTime(seconds) {
  if (seconds === null || seconds === undefined) return 'Calculating';
  const value = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(value / 60);
  const remainder = value % 60;
  return `${minutes}:${String(remainder).padStart(2,'0')}`;
}

function render(job) {
  const percent = Number(job.percent || 0);
  bar.style.width = `${percent}%`;
  percentEl.textContent = `${percent}%`;
  stageEl.textContent = job.stage || 'Working';
  elapsedEl.textContent = formatTime(job.elapsedSeconds || 0);
  etaEl.textContent = percent >= 100 ? '0:00' : formatTime(job.estimatedSecondsRemaining);

  const stats = job.stats || {};
  regionsEl.textContent = stats.finalRegions ?? stats.regionCandidates ?? stats.vectorized ?? '—';
  inkStatsEl.textContent = stats.inkPaths ?? stats.markerSeeds ?? '—';
  coveragePercentEl.textContent =
    stats.pixelCoveragePercent !== undefined
      ? `${stats.pixelCoveragePercent}%`
      : 'Checking…';
  coverageErrorsEl.textContent =
    stats.coverageErrors !== undefined ? String(stats.coverageErrors) : '—';
  orphanPixelsEl.textContent =
    stats.orphanPixels !== undefined ? String(stats.orphanPixels) : '—';
  edgeRepairEl.textContent =
    stats.edgeRepair === 'completed' ? 'Completed ✓' : 'Pending';

  logsEl.innerHTML = (job.logs || []).map(log =>
    `<div>[${log.time}] ${log.percent}% — ${log.message}</div>`
  ).join('');
  logsEl.scrollTop = logsEl.scrollHeight;

  if (job.status === 'complete') {
    statusMessage.className = 'success';
    statusMessage.textContent = `Completed successfully${job.metadata?.regions ? ` with ${job.metadata.regions} regions` : ''}.`;
    cancelBtn.classList.add('hidden');
    downloadBtn.classList.remove('hidden');
    testBtn.classList.remove('hidden');
    qaBtn.classList.remove('hidden');
    downloadBtn.href = `/jobs/${job.id}/download`;
    testBtn.href = `/jobs/${job.id}/test`;
    qaBtn.href = `/jobs/${job.id}/qa`;
    compileBtn.disabled = false;

    if (!autoDownloaded) {
      autoDownloaded = true;
      window.location.href = downloadBtn.href;
    }
  } else if (job.status === 'failed') {
    statusMessage.className = 'error';
    statusMessage.textContent = job.error || 'Compilation failed.';
    cancelBtn.classList.add('hidden');
    compileBtn.disabled = false;
  } else if (job.status === 'cancelled') {
    statusMessage.className = 'error';
    statusMessage.textContent = 'Compilation cancelled.';
    cancelBtn.classList.add('hidden');
    compileBtn.disabled = false;
  } else {
    statusMessage.className = '';
    statusMessage.textContent = 'Please keep this page open while the compiler works.';
  }
}

async function pollJob() {
  if (!activeJobId) return;
  try {
    const response = await fetch(`/jobs/${activeJobId}`);
    if (!response.ok) throw new Error('Could not read job progress.');
    const job = await response.json();
    render(job);
    if (!['complete','failed','cancelled'].includes(job.status)) {
      pollTimer = setTimeout(pollJob, 700);
    }
  } catch (error) {
    statusMessage.className = 'error';
    statusMessage.textContent = error.message;
    pollTimer = setTimeout(pollJob, 1500);
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  clearTimeout(pollTimer);
  autoDownloaded = false;
  compileBtn.disabled = true;
  cancelBtn.classList.remove('hidden');
  downloadBtn.classList.add('hidden');
  testBtn.classList.add('hidden');
  qaBtn.classList.add('hidden');
  panel.classList.remove('hidden');
  logsEl.innerHTML = '';
  render({percent:0,stage:'Uploading artwork',status:'queued',logs:[]});

  const formData = new FormData(form);
  try {
    const response = await fetch('/jobs', {method:'POST', body:formData});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Unable to start compilation.');
    activeJobId = result.jobId;
    pollJob();
  } catch (error) {
    compileBtn.disabled = false;
    statusMessage.className = 'error';
    statusMessage.textContent = error.message;
  }
});

cancelBtn.addEventListener('click', async () => {
  if (!activeJobId) return;
  cancelBtn.disabled = true;
  await fetch(`/jobs/${activeJobId}/cancel`, {method:'POST'});
  cancelBtn.disabled = false;
});
</script>
</body>
</html>
"""


@app.get("/samples", response_class=HTMLResponse)
def sample_library():
    path = Path(__file__).parent / "samples" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))



@app.get("/samples/covers/{filename}")
def sample_cover(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        raise HTTPException(status_code=404, detail="Cover image not found.")

    covers_dir = (Path(__file__).parent / "samples" / "covers").resolve()
    path = (covers_dir / safe_name).resolve()

    if path.parent != covers_dir or not path.is_file():
        raise HTTPException(status_code=404, detail="Cover image not found.")

    return FileResponse(path)



PREMIUM_SAMPLE_SETTINGS = PREMIUM_CATALOG

def premium_paths(sample_name: str) -> tuple[Path, Path, Path]:
    base = PREMIUM_GENERATED_ROOT / sample_name
    output_dir = base / "package"
    zip_path = base / f"{sample_name}_paint_package.zip"
    return base, output_dir, zip_path


def ensure_directory(path: Path, attempts: int = 4) -> None:
    """Create a directory reliably, including on OneDrive-synced Windows folders."""
    last_error = None
    for attempt in range(attempts):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except FileNotFoundError as exc:
            last_error = exc
            # OneDrive can briefly report a missing parent immediately after deletion.
            path.parent.mkdir(parents=True, exist_ok=True)
            time.sleep(0.15 * (attempt + 1))
        except OSError as exc:
            last_error = exc
            time.sleep(0.15 * (attempt + 1))
    if last_error:
        raise last_error


def premium_launcher_html(sample_name: str, item: dict[str, Any]) -> str:
    title = item["title"]
    difficulty = item["difficulty"]
    colors = item["colors"]
    regions = item["target_regions"]
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Premium Paint-by-Number</title>
<style>
:root{{--gold:#e1ad4a;--purple:#8d42bc;--cream:#f5e8d6;--line:#604527}}
*{{box-sizing:border-box}}body{{margin:0;background:#090807;color:var(--cream);font-family:Arial,sans-serif}}
.wrap{{min-height:100vh;display:grid;grid-template-columns:minmax(0,1.5fr) minmax(330px,.5fr)}}
.preview{{position:relative;background:#050505;display:grid;place-items:center;padding:20px}}
.preview img{{max-width:100%;max-height:92vh;object-fit:contain;border:1px solid #6c4c28;box-shadow:0 24px 70px #000}}
.panel{{padding:30px;background:linear-gradient(180deg,#11100e,#090807);border-left:1px solid var(--line)}}
.back{{color:var(--gold);text-decoration:none;font-weight:800}}h1{{font-family:Georgia,serif;font-size:39px;line-height:1.05;margin:25px 0 8px}}
.sub{{color:#c9b79e;line-height:1.55}}.stats{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:20px 0}}
.stat{{border:1px solid #4f3924;border-radius:12px;padding:13px;background:#15110d}}.stat small{{display:block;color:#ae9d87;margin-bottom:5px}}
.progress{{height:14px;border-radius:999px;background:#30251a;overflow:hidden;margin:13px 0}}.bar{{height:100%;width:0;background:linear-gradient(90deg,var(--purple),var(--gold));transition:width .35s}}
.log{{min-height:86px;padding:14px;border-radius:12px;background:#050505;border:1px solid #39291b;color:#d7c6af;line-height:1.5}}
.btn{{display:block;width:100%;padding:14px;margin-top:13px;border-radius:11px;border:1px solid #644724;background:#17110c;color:#fff;font-weight:900;text-align:center;text-decoration:none;cursor:pointer}}
.primary{{background:var(--purple);border-color:var(--purple)}}.hidden{{display:none}}.notice{{padding:12px;border-radius:10px;background:#24162a;border:1px solid #653c75;color:#ead5f3;margin-top:15px}}
@media(max-width:850px){{.wrap{{grid-template-columns:1fr}}.preview{{min-height:45vh}}.panel{{border-left:0;border-top:1px solid var(--line)}}}}
</style></head>
<body><div class="wrap"><section class="preview">
<img src="/samples/covers/{sample_name}.jpg" alt="{title}">
</section><aside class="panel"><a class="back" href="/samples">← Premium Library</a>
<h1>{title}</h1><p class="sub">The official detailed artwork is being converted into a real interactive paint-by-number. This page no longer uses the old flat placeholder SVG.</p>
<div class="stats"><div class="stat"><small>Difficulty</small><strong>{difficulty}</strong></div>
<div class="stat"><small>Palette</small><strong>{colors} colors</strong></div>
<div class="stat"><small>Target detail</small><strong>Up to {regions} regions</strong></div>
<div class="stat"><small>Source</small><strong>Official artwork</strong></div></div>
<div class="progress"><div id="bar" class="bar"></div></div>
<div id="log" class="log">Preparing the detailed paint-by-number compiler…</div>
<button id="start" class="btn primary">Build This Paint-by-Number</button>
<a id="open" class="btn primary hidden" href="/premium/{sample_name}/play">Open Interactive Painting</a>
<a id="download" class="btn hidden" href="#">Download Paint Package</a>
<div class="notice">The first build can take several minutes because it is generating hundreds of real regions from the official artwork. After it finishes, the result is cached.</div>
</aside></div>
<script>
const slug={json.dumps(sample_name)};
const startBtn=document.getElementById('start');
const openBtn=document.getElementById('open');
const downloadBtn=document.getElementById('download');
const bar=document.getElementById('bar');
const log=document.getElementById('log');
let jobId=null;
async function begin(){{
 startBtn.disabled=true;startBtn.textContent='Starting detailed compiler…';
 const response=await fetch(`/api/premium/${{slug}}/compile`,{{method:'POST'}});
 const data=await response.json();
 if(!response.ok){{log.textContent=data.detail||'Unable to start compilation.';startBtn.disabled=false;return}}
 if(data.ready){{showReady(data);return}}
 jobId=data.jobId;poll();
}}
function showReady(data){{
 bar.style.width='100%';log.textContent='Detailed premium paint-by-number is ready.';
 startBtn.classList.add('hidden');openBtn.classList.remove('hidden');
 downloadBtn.classList.remove('hidden');
 downloadBtn.href=data.downloadUrl||`/premium/${{slug}}/download`;
}}
async function poll(){{
 const response=await fetch(`/jobs/${{jobId}}`);
 const data=await response.json();
 bar.style.width=`${{data.percent||0}}%`;
 log.textContent=`${{data.percent||0}}% — ${{data.stage||'Processing artwork'}}`;
 if(data.status==='complete'){{showReady({{downloadUrl:`/jobs/${{jobId}}/download`}});return}}
 if(data.status==='failed'||data.status==='cancelled'){{log.textContent=data.error||data.stage;startBtn.disabled=false;startBtn.textContent='Try Again';return}}
 setTimeout(poll,1200);
}}
startBtn.onclick=begin;
fetch(`/api/premium/${{slug}}/status`).then(r=>r.json()).then(data=>{{if(data.ready)showReady(data)}});
</script></body></html>"""


@app.get("/samples/{sample_name}", response_class=HTMLResponse)
def sample_player(sample_name: str):
    safe_name = sample_name.strip().lower()
    item = PREMIUM_SAMPLE_SETTINGS.get(safe_name)
    if not item:
        raise HTTPException(status_code=404, detail="Premium design not found.")
    return HTMLResponse(premium_launcher_html(safe_name, item))


@app.get("/api/premium/{sample_name}/status")
def premium_status(sample_name: str):
    if sample_name not in PREMIUM_SAMPLE_SETTINGS:
        raise HTTPException(status_code=404, detail="Premium design not found.")
    _, output_dir, zip_path = premium_paths(sample_name)
    player = output_dir / "interactive_player.html"
    return {
        "ready": player.is_file(),
        "playUrl": f"/premium/{sample_name}/play",
        "downloadUrl": f"/premium/{sample_name}/download" if zip_path.is_file() else None,
    }


@app.post("/api/premium/{sample_name}/compile")
def compile_premium_sample(sample_name: str):
    item = PREMIUM_SAMPLE_SETTINGS.get(sample_name)
    if not item:
        raise HTTPException(status_code=404, detail="Premium design not found.")

    base, output_dir, zip_path = premium_paths(sample_name)
    player = output_dir / "interactive_player.html"
    if player.is_file():
        return {
            "ready": True,
            "playUrl": f"/premium/{sample_name}/play",
            "downloadUrl": f"/premium/{sample_name}/download",
        }

    with jobs_lock:
        for existing_id, existing in jobs.items():
            if (
                existing.get("premiumSample") == sample_name
                and existing.get("status") in {"queued", "running"}
            ):
                return {"ready": False, "jobId": existing_id}

    cover_path = Path(__file__).parent / "samples" / "covers" / f"{sample_name}.jpg"
    if not cover_path.is_file():
        raise HTTPException(status_code=404, detail="Official premium artwork file is missing.")

    # Keep the stable LocalAppData parent and reset only generated contents.
    premium_root = base.parent
    ensure_directory(premium_root)
    ensure_directory(base)

    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    if zip_path.exists():
        zip_path.unlink(missing_ok=True)

    ensure_directory(output_dir)

    job_id = uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "percent": 0,
        "stage": "Queued premium artwork",
        "stats": {},
        "logs": [],
        "elapsedSeconds": 0,
        "estimatedSecondsRemaining": None,
        "downloadReady": False,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "workDir": str(base),
        "inputPath": str(cover_path),
        "outputDir": str(output_dir),
        "zipPath": str(zip_path),
        "cancelRequested": False,
        "premiumSample": sample_name,
    }

    with jobs_lock:
        jobs[job_id] = job
    RUNTIME_DB.save_job(job)

    settings = {
        "preset": "illustration",
        "design_style": "smart_auto",
        "colors": item["colors"],
        "min_region_area": 24,
        "experience_mode": "relaxed",
        "target_regions": item["target_regions"],
        "outline_width": 0.24,
        "simplify_tolerance": 0.18,
        "auto_crop": False,
        "generate_pdf": True,
        "finish_mode": "original",
    }
    executor.submit(run_job, job_id, settings)
    return {"ready": False, "jobId": job_id}


@app.get("/premium/{sample_name}/play", response_class=HTMLResponse)
def play_premium_sample(sample_name: str):
    if sample_name not in PREMIUM_SAMPLE_SETTINGS:
        raise HTTPException(status_code=404, detail="Premium design not found.")
    _, output_dir, _ = premium_paths(sample_name)
    player = output_dir / "interactive_player.html"
    if not player.is_file():
        raise HTTPException(status_code=409, detail="This premium design has not been compiled yet.")
    return HTMLResponse(player.read_text(encoding="utf-8"))


@app.get("/premium/{sample_name}/download")
def download_premium_sample(sample_name: str):
    if sample_name not in PREMIUM_SAMPLE_SETTINGS:
        raise HTTPException(status_code=404, detail="Premium design not found.")
    _, _, zip_path = premium_paths(sample_name)
    if not zip_path.is_file():
        raise HTTPException(status_code=409, detail="This premium package has not been compiled yet.")
    return FileResponse(
        zip_path,
        filename=f"{sample_name}_premium_paint_by_number.zip",
        media_type="application/zip",
    )


def start_job(input_bytes: bytes, filename: str | None, settings: dict[str, Any]) -> str:
    """Persist an uploaded artwork and queue it for compilation."""
    job_id = uuid4().hex
    work_dir = RUNTIME_DATA_DIR / "jobs" / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = work_dir / (filename or "artwork.png")
    output_dir = work_dir / "package"
    zip_path = work_dir / "euqilegna_paint_package_v7.zip"

    output_dir.mkdir()
    input_path.write_bytes(input_bytes)

    job = {
        "id": job_id,
        "status": "queued",
        "percent": 0,
        "stage": "Queued",
        "stats": {},
        "logs": [],
        "elapsedSeconds": 0,
        "estimatedSecondsRemaining": None,
        "downloadReady": False,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "workDir": str(work_dir),
        "inputPath": str(input_path),
        "outputDir": str(output_dir),
        "zipPath": str(zip_path),
        "cancelRequested": False,
    }

    with jobs_lock:
        jobs[job_id] = job
    RUNTIME_DB.save_job(job)

    executor.submit(run_job, job_id, settings)
    return job_id


@app.post("/jobs")
async def create_job(
    file: UploadFile = File(...),
    preset: str = Form("illustration"),
    design_style: str = Form("smart_auto"),
    colors: int = Form(40),
    min_region_area: int = Form(38),
    experience_mode: str = Form("relaxed"),
    target_regions: int = Form(650),
    outline_width: float = Form(0.38),
    simplify_tolerance: float = Form(0.35),
    auto_crop: bool = Form(True),
    generate_pdf: bool = Form(True),
    finish_mode: str = Form("original"),
):
    settings = {
        "preset": preset,
        "design_style": design_style,
        "colors": colors,
        "min_region_area": min_region_area,
        "experience_mode": experience_mode,
        "target_regions": target_regions,
        "merge_strength": 0,
        "outline_width": outline_width,
        "simplify_tolerance": simplify_tolerance,
        "smoothing_passes": 0,
        "auto_crop": auto_crop,
        "exclude_background": True,
        "generate_pdf": generate_pdf,
        "finish_mode": finish_mode,
    }

    job_id = start_job(await file.read(), file.filename, settings)
    return {"jobId": job_id}


@app.post("/api/paintings/upload")
async def upload_painting(file: UploadFile = File(...)):
    """Compile an uploaded image into a painting for the Color-by-Number canvas."""
    job_id = start_job(await file.read(), file.filename, dict(BETA_SETTINGS))
    return {"jobId": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = get_job_record(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Compilation job not found.")
    return public_job(job)


@app.get("/jobs/{job_id}/artwork")
def get_job_artwork(job_id: str):
    """Compiled regions and palette for rendering a compiled job as a canvas."""
    job = get_job_record(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    output_dir = Path(job["outputDir"])
    regions_path = output_dir / "regions.json"
    palette_path = output_dir / "palette.json"
    if not regions_path.exists() or not palette_path.exists():
        raise HTTPException(status_code=409, detail="No compiled artwork for this job yet.")

    metadata_path = output_dir / "metadata.json"
    metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_path.exists()
        else {}
    )

    return {
        "jobId": job_id,
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "regionCount": metadata.get("regions"),
        "colorCount": metadata.get("colors"),
        "palette": json.loads(palette_path.read_text(encoding="utf-8")),
        "regions": json.loads(regions_path.read_text(encoding="utf-8")),
    }


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = get_job_record(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Compilation job not found.")

    if job["status"] in {"complete", "failed", "cancelled", "interrupted"}:
        return {"ok": True, "status": job["status"]}

    with jobs_lock:
        jobs[job_id]["cancelRequested"] = True
        jobs[job_id]["stage"] = "Cancellation requested"
        jobs[job_id]["updatedAt"] = utc_now()
        snapshot = dict(jobs[job_id])

    RUNTIME_DB.save_job(snapshot)
    return {"ok": True, "status": "cancelling"}



@app.get("/jobs/{job_id}/test", response_class=HTMLResponse)
def test_job(job_id: str):
    job = get_job_record(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Compilation job not found.")
        if job["status"] != "complete":
            raise HTTPException(status_code=409, detail="Compilation is not complete.")
        player_path = Path(job["outputDir"]) / "interactive_player.html"

    if not player_path.exists():
        raise HTTPException(status_code=404, detail="Interactive test player was not generated.")

    return HTMLResponse(player_path.read_text(encoding="utf-8"))




@app.get("/jobs/{job_id}/qa", response_class=HTMLResponse)
def qa_job(job_id: str):
    job = get_job_record(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Compilation job not found.")
        if job["status"] != "complete":
            raise HTTPException(status_code=409, detail="Compilation is not complete.")
        qa_path = Path(job["outputDir"]) / "quality_dashboard.html"

    if not qa_path.exists():
        raise HTTPException(status_code=404, detail="Quality report was not generated.")

    return HTMLResponse(qa_path.read_text(encoding="utf-8"))



@app.get("/jobs/{job_id}/download")
def download_job(job_id: str):
    job = get_job_record(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Compilation job not found.")
        if job["status"] != "complete":
            raise HTTPException(status_code=409, detail="Compilation is not complete.")
        zip_path = Path(job["zipPath"])

    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Generated package was not found.")

    return FileResponse(
        zip_path,
        filename="euqilegna_paint_package_v7.zip",
        media_type="application/zip",
    )
