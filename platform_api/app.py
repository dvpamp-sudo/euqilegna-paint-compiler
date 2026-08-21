from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from uuid import uuid4
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from euqilegna_engine import CompilerOrchestrator, CompileRequest
from euqilegna_engine.exporter import export_zip
from euqilegna_engine.orchestrator import CompilationQualityError


app = FastAPI(title="Euqilegna Modular Compiler Platform 10.0.1")
engine = CompilerOrchestrator()
executor = ThreadPoolExecutor(max_workers=2)
jobs: dict[str, dict] = {}
jobs_lock = Lock()


def update_job(job_id: str, **updates) -> None:
    with jobs_lock:
        jobs[job_id].update(updates)


def run_job(job_id: str, request: CompileRequest, zip_path: Path) -> None:
    update_job(job_id, status="running", percent=1, stage="Starting modular compiler")

    def progress(percent: int, stage: str, stats: dict) -> None:
        update_job(job_id, percent=percent, stage=stage, stats=stats)

    try:
        result = engine.compile(request, progress_callback=progress)
        export_zip(result.output_dir, zip_path)
        update_job(
            job_id,
            status="complete",
            percent=100,
            stage="Validated and ready",
            quality=result.quality_report.to_dict(),
            downloadReady=True,
        )
    except CompilationQualityError as exc:
        update_job(
            job_id,
            status="quality_failed",
            stage="Export blocked by quality control",
            error=str(exc),
        )
    except Exception as exc:
        update_job(job_id, status="failed", stage="Compilation failed", error=str(exc))


@app.get("/", response_class=HTMLResponse)
def platform_home():
    path = Path(__file__).resolve().parents[1] / "platform_ui" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/health")
def health():
    return {
        "ok": True,
        "application": "Euqilegna Modular Compiler Platform",
        "version": "10.0.1",
    }


@app.post("/api/compile")
async def compile_image(
    file: UploadFile = File(...),
    colors: int = Form(30),
    target_regions: int = Form(600),
    min_region_area: int = Form(24),
    finish_mode: str = Form("original"),
):
    workspace = Path(tempfile.mkdtemp(prefix="euqilegna_platform_"))
    input_path = workspace / (file.filename or "artwork.png")
    output_dir = workspace / "package"
    zip_path = workspace / "euqilegna_validated_package.zip"
    input_path.write_bytes(await file.read())

    job_id = uuid4().hex
    request = CompileRequest(
        input_path=input_path,
        output_dir=output_dir,
        colors=max(8, min(48, colors)),
        target_regions=max(120, min(1800, target_regions)),
        min_region_area=max(8, min(120, min_region_area)),
        finish_mode=finish_mode,
    )

    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "percent": 0,
            "stage": "Queued",
            "downloadReady": False,
            "zipPath": str(zip_path),
            "outputDir": str(output_dir),
        }

    executor.submit(run_job, job_id, request, zip_path)
    return {"jobId": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return {
            key: value
            for key, value in job.items()
            if key not in {"zipPath", "outputDir"}
        }


@app.get("/api/jobs/{job_id}/play", response_class=HTMLResponse)
def play_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "complete":
            raise HTTPException(status_code=409, detail="Validated player is not ready.")
        path = Path(job["outputDir"]) / "interactive_player.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/quality")
def quality_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job.get("quality") or {"passed": False, "message": "Quality report is not ready."}


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "complete":
            raise HTTPException(status_code=409, detail="Validated package is not ready.")
        path = Path(job["zipPath"])
    return FileResponse(path, filename="euqilegna_validated_package.zip")
