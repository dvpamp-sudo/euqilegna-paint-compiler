from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
import csv
import io
import json
import os
import secrets
import sqlite3

from canvas_progress import CanvasProgress
from paintings import Painting, PaintingStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_data_dir(project_root: Path) -> Path:
    """
    Use a Render persistent disk when DATA_DIR is configured.
    Local development falls back to ./runtime_data.
    """
    configured = os.getenv("EUQILEGNA_DATA_DIR") or os.getenv("DATA_DIR")
    if configured:
        path = Path(configured)
    elif Path("/var/data").exists():
        path = Path("/var/data/euqilegna")
    else:
        path = project_root / "runtime_data"

    path.mkdir(parents=True, exist_ok=True)
    (path / "jobs").mkdir(exist_ok=True)
    (path / "packages").mkdir(exist_ok=True)
    (path / "uploads").mkdir(exist_ok=True)
    return path


class RuntimeDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Lock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    percent INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'Queued',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS beta_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submitted_at TEXT NOT NULL,
                    tester_name TEXT,
                    device TEXT,
                    artwork TEXT,
                    completion_status TEXT,
                    enjoyment INTEGER,
                    ease_of_use INTEGER,
                    recommend INTEGER,
                    pointer_worked INTEGER,
                    save_worked INTEGER,
                    hint_worked INTEGER,
                    comments TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS beta_invites (
                    code TEXT PRIMARY KEY,
                    label TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS paintings (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    date_created TEXT NOT NULL,
                    medium TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    source_image TEXT,
                    package_dir TEXT,
                    svg_path TEXT,
                    player_path TEXT,
                    region_count INTEGER,
                    color_count INTEGER
                );

                CREATE TABLE IF NOT EXISTS canvas_sessions (
                    session_id TEXT NOT NULL,
                    artwork_key TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 1,
                    completed_json TEXT NOT NULL DEFAULT '[]',
                    region_total INTEGER,
                    saves INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, artwork_key)
                );
                """
            )
            self._ensure_columns(
                connection,
                "paintings",
                {
                    "source_image": "TEXT",
                    "package_dir": "TEXT",
                    "svg_path": "TEXT",
                    "player_path": "TEXT",
                    "region_count": "INTEGER",
                    "color_count": "INTEGER",
                },
            )

    def _ensure_columns(
        self,
        connection: sqlite3.Connection,
        table: str,
        columns: dict[str, str],
    ) -> None:
        """Add columns that were introduced after a database was first created."""
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def save_job(self, job: dict[str, Any]) -> None:
        # Persist the full internal job record so another process or a restarted
        # Render instance can reconstruct the job. The HTTP API still filters
        # private filesystem fields through main.public_job().
        public = dict(job)
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(id,status,percent,stage,created_at,updated_at,payload_json)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    percent=excluded.percent,
                    stage=excluded.stage,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    job["id"],
                    job.get("status", "queued"),
                    int(job.get("percent", 0)),
                    job.get("stage", "Queued"),
                    job.get("createdAt", utc_now()),
                    job.get("updatedAt", utc_now()),
                    json.dumps(public, ensure_ascii=False),
                ),
            )

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        """Load one persisted job by ID.

        This is the authoritative fallback when an in-memory job dictionary is
        empty because a Render process was restarted or a request reached a
        different process.
        """
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id,status,percent,stage,created_at,updated_at,payload_json
                FROM jobs
                WHERE id=?
                """,
                (job_id,),
            ).fetchone()

        if row is None:
            return None

        payload = json.loads(row["payload_json"] or "{}")
        payload.setdefault("id", row["id"])
        payload["status"] = row["status"]
        payload["percent"] = int(row["percent"])
        payload["stage"] = row["stage"]
        payload.setdefault("createdAt", row["created_at"])
        payload["updatedAt"] = row["updated_at"]
        payload.setdefault("logs", [])
        payload.setdefault("stats", {})
        payload.setdefault("cancelRequested", False)
        return payload

    def set_cancel_requested(self, job_id: str, requested: bool = True) -> dict[str, Any] | None:
        job = self.load_job(job_id)
        if job is None:
            return None
        job["cancelRequested"] = bool(requested)
        job["stage"] = "Cancellation requested" if requested else job.get("stage", "Queued")
        job["updatedAt"] = utc_now()
        self.save_job(job)
        return job

    def mark_interrupted_jobs(self) -> int:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT id,payload_json FROM jobs WHERE status IN ('queued','running')"
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"] or "{}")
                payload.update(
                    {
                        "status": "interrupted",
                        "stage": "Interrupted by a service restart — restart compilation",
                        "updatedAt": utc_now(),
                    }
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='interrupted',
                        stage=?,
                        updated_at=?,
                        payload_json=?
                    WHERE id=?
                    """,
                    (
                        payload["stage"],
                        payload["updatedAt"],
                        json.dumps(payload),
                        row["id"],
                    ),
                )
            return len(rows)

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM jobs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload_json"] or "{}") for row in rows]

    def save_painting(self, painting: Painting) -> Painting:
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO paintings(
                    id,title,date_created,medium,status,
                    source_image,package_dir,svg_path,player_path,
                    region_count,color_count
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    date_created=excluded.date_created,
                    medium=excluded.medium,
                    status=excluded.status,
                    source_image=excluded.source_image,
                    package_dir=excluded.package_dir,
                    svg_path=excluded.svg_path,
                    player_path=excluded.player_path,
                    region_count=excluded.region_count,
                    color_count=excluded.color_count
                """,
                (
                    painting.id,
                    painting.title,
                    painting.date_created,
                    painting.medium,
                    str(painting.status),
                    painting.source_image,
                    painting.package_dir,
                    painting.svg_path,
                    painting.player_path,
                    painting.region_count,
                    painting.color_count,
                ),
            )
        return painting

    def load_painting(self, painting_id: str) -> Painting | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id,title,date_created,medium,status,
                       source_image,package_dir,svg_path,player_path,
                       region_count,color_count
                FROM paintings
                WHERE id=?
                """,
                (painting_id,),
            ).fetchone()
        return _row_to_painting(row) if row else None

    def list_paintings(self, status: str | None = None) -> list[Painting]:
        query = (
            "SELECT id,title,date_created,medium,status,"
            "source_image,package_dir,svg_path,player_path,"
            "region_count,color_count FROM paintings"
        )
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status=?"
            params = (str(status),)
        query += " ORDER BY date_created DESC, title ASC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_to_painting(row) for row in rows]

    def save_canvas_progress(self, progress: CanvasProgress) -> CanvasProgress:
        """Store a session's canvas state, creating the row or updating it in place."""
        now = utc_now()
        with self.lock, self.connect() as connection:
            existing = connection.execute(
                """
                SELECT created_at,saves FROM canvas_sessions
                WHERE session_id=? AND artwork_key=?
                """,
                (progress.session_id, progress.artwork_key),
            ).fetchone()
            progress.created_at = existing["created_at"] if existing else now
            progress.saves = int(existing["saves"]) + 1 if existing else 1
            progress.updated_at = now
            connection.execute(
                """
                INSERT INTO canvas_sessions(
                    session_id,artwork_key,selected,completed_json,
                    region_total,saves,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,artwork_key) DO UPDATE SET
                    selected=excluded.selected,
                    completed_json=excluded.completed_json,
                    region_total=excluded.region_total,
                    saves=excluded.saves,
                    updated_at=excluded.updated_at
                """,
                (
                    progress.session_id,
                    progress.artwork_key,
                    int(progress.selected),
                    json.dumps(list(progress.completed)),
                    progress.region_total,
                    progress.saves,
                    progress.created_at,
                    progress.updated_at,
                ),
            )
        return progress

    def load_canvas_session(self, session_id: str, limit: int = 20) -> list[CanvasProgress]:
        """Every artwork this session painted, most recently saved first."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id,artwork_key,selected,completed_json,
                       region_total,saves,created_at,updated_at
                FROM canvas_sessions
                WHERE session_id=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [_row_to_canvas_progress(row) for row in rows]

    def list_canvas_sessions(self, limit: int = 200) -> list[CanvasProgress]:
        """Every saved canvas session, most recently painted first."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id,artwork_key,selected,completed_json,
                       region_total,saves,created_at,updated_at
                FROM canvas_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_canvas_progress(row) for row in rows]

    def save_feedback(self, payload: dict[str, Any]) -> int:
        submitted_at = payload.get("submittedAt") or utc_now()
        with self.lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO beta_feedback(
                    submitted_at,tester_name,device,artwork,completion_status,
                    enjoyment,ease_of_use,recommend,pointer_worked,save_worked,
                    hint_worked,comments,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    submitted_at,
                    payload.get("testerName"),
                    payload.get("device"),
                    payload.get("artwork"),
                    payload.get("completionStatus"),
                    _as_int(payload.get("enjoyment")),
                    _as_int(payload.get("easeOfUse")),
                    _as_int(payload.get("recommend")),
                    _as_bool_int(payload.get("pointerWorked")),
                    _as_bool_int(payload.get("saveWorked")),
                    _as_bool_int(payload.get("hintWorked")),
                    payload.get("comments"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def feedback_rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM beta_feedback ORDER BY submitted_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_invite(self, label: str | None = None) -> str:
        code = secrets.token_urlsafe(7).replace("-", "").replace("_", "")[:10]
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO beta_invites(code,label,enabled,created_at,uses)
                VALUES(?,?,1,?,0)
                """,
                (code, label or "Family & Friends", utc_now()),
            )
        return code

    def ensure_default_invite(self) -> str:
        configured = os.getenv("EUQILEGNA_BETA_CODE", "").strip()
        if configured:
            with self.lock, self.connect() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO beta_invites(code,label,enabled,created_at,uses)
                    VALUES(?,?,1,?,0)
                    """,
                    (configured, "Configured Beta Code", utc_now()),
                )
            return configured

        with self.connect() as connection:
            row = connection.execute(
                "SELECT code FROM beta_invites WHERE enabled=1 ORDER BY created_at LIMIT 1"
            ).fetchone()
        if row:
            return str(row["code"])
        return self.create_invite()

    def validate_invite(self, code: str) -> bool:
        if not code:
            return False
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM beta_invites WHERE code=?",
                (code,),
            ).fetchone()
            if not row or not row["enabled"]:
                return False
            connection.execute(
                "UPDATE beta_invites SET uses=uses+1 WHERE code=?",
                (code,),
            )
            return True


def _row_to_canvas_progress(row: sqlite3.Row) -> CanvasProgress:
    try:
        completed = tuple(int(value) for value in json.loads(row["completed_json"] or "[]"))
    except (TypeError, ValueError):
        completed = ()
    return CanvasProgress(
        session_id=row["session_id"],
        artwork_key=row["artwork_key"],
        selected=int(row["selected"] or 1),
        completed=completed,
        region_total=row["region_total"],
        saves=int(row["saves"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_painting(row: sqlite3.Row) -> Painting:
    return Painting(
        id=row["id"],
        title=row["title"],
        date_created=row["date_created"],
        medium=row["medium"],
        status=PaintingStatus(row["status"]),
        source_image=row["source_image"],
        package_dir=row["package_dir"],
        svg_path=row["svg_path"],
        player_path=row["player_path"],
        region_count=row["region_count"],
        color_count=row["color_count"],
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if str(value).lower() in {"1", "true", "yes", "on"} else 0


def feedback_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    fields = [
        "submitted_at", "tester_name", "device", "artwork",
        "completion_status", "enjoyment", "ease_of_use", "recommend",
        "pointer_worked", "save_worked", "hint_worked", "comments",
    ]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def render_beta_portal(public_url: str, beta_code: str) -> str:
    escaped_url = public_url.rstrip("/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Euqilegna Family & Friends Beta</title>
<style>
:root{{--bg:#09070a;--panel:#151117;--line:#5b4024;--gold:#e0ad4f;--purple:#9445c1;--cream:#f5e8d7;--muted:#cbb9a4}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at top right,#32153d,transparent 36%),var(--bg);color:var(--cream);font-family:Arial,sans-serif}}
main{{max-width:980px;margin:auto;padding:22px}} h1,h2{{font-family:Georgia,serif}} h1{{font-size:clamp(38px,8vw,72px);line-height:.98;margin:18px 0}}
.hero,.card{{background:rgba(20,15,21,.94);border:1px solid var(--line);border-radius:22px;padding:22px}} .hero p{{color:var(--muted);font-size:18px;line-height:1.55}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:16px}} .card h2{{margin-top:0}} .step{{color:var(--gold);font-weight:900}}
a.button,button{{display:inline-block;background:var(--purple);color:#fff;text-decoration:none;border:0;border-radius:12px;padding:14px 18px;font-weight:900;margin:6px 8px 6px 0}}
a.secondary{{background:#221922;border:1px solid var(--line)}} code{{color:var(--gold)}} .notice{{border-left:4px solid var(--gold);padding:12px;background:#0c090d;border-radius:8px}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}} main{{padding:14px}}}}
</style></head>
<body><main>
<section class="hero">
<div class="step">PRIVATE FAMILY & FRIENDS BETA</div>
<h1>Help shape the Euqilegna Art Studio.</h1>
<p>Test the real interactive painting experience on iPad, phone, or computer. Paint a few sections, try zooming, use a hint, save progress, and tell us what worked or felt confusing.</p>
<div class="notice">Beta access code: <strong>{beta_code}</strong></div>
<a class="button" href="{escaped_url}/">Open the Studio</a>
<a class="button secondary" href="{escaped_url}/beta/feedback-v10?code={beta_code}">Leave Feedback</a>
</section>
<div class="grid">
<section class="card"><div class="step">1. START</div><h2>Choose artwork</h2><p>Open a Premium Library painting or upload a test image.</p></section>
<section class="card"><div class="step">2. PAINT</div><h2>Test interaction</h2><p>Select a number, tap matching regions, zoom, pan, and try a hint.</p></section>
<section class="card"><div class="step">3. SAVE</div><h2>Resume progress</h2><p>Save, refresh the browser, and confirm the painting restores correctly.</p></section>
<section class="card"><div class="step">4. REVIEW</div><h2>Tell us honestly</h2><p>Report pointer problems, invisible regions, confusing controls, and what you enjoyed.</p></section>
</div>
</main></body></html>"""


def render_feedback_form(beta_code: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Euqilegna Beta Feedback</title>
<style>
body{{margin:0;background:#0a080a;color:#f5e8d7;font-family:Arial}} main{{max-width:760px;margin:auto;padding:20px}}
form{{background:#171217;border:1px solid #5b4024;border-radius:20px;padding:22px}} label{{display:block;margin:15px 0 6px;font-weight:800}}
input,select,textarea{{width:100%;padding:12px;border-radius:10px;border:1px solid #5b4024;background:#0c090d;color:#fff;font-size:16px}}
fieldset{{border:1px solid #5b4024;border-radius:12px;margin:16px 0;padding:12px}} button{{padding:14px 18px;border:0;border-radius:11px;background:#9445c1;color:#fff;font-weight:900;font-size:16px}}
small{{color:#cbb9a4}} .checks label{{font-weight:normal}}
</style></head><body><main><h1>Family & Friends Beta Feedback</h1>
<form id="feedback">
<input type="hidden" name="code" value="{beta_code}">
<label>Your name</label><input name="testerName" required>
<label>Device</label><select name="device"><option>iPad</option><option>iPhone</option><option>Android tablet</option><option>Android phone</option><option>Windows computer</option><option>Mac</option></select>
<label>Artwork tested</label><input name="artwork" required>
<label>How far did you get?</label><select name="completionStatus"><option>Tested a few sections</option><option>Reached 25%</option><option>Reached 50%</option><option>Reached 75%</option><option>Completed it</option><option>Could not continue</option></select>
<label>Enjoyment (1–5)</label><input name="enjoyment" type="number" min="1" max="5" required>
<label>Ease of use (1–5)</label><input name="easeOfUse" type="number" min="1" max="5" required>
<label>Would you recommend it? (1–10)</label><input name="recommend" type="number" min="1" max="10" required>
<fieldset class="checks"><legend>Feature checks</legend>
<label><input type="checkbox" name="pointerWorked"> Selecting a number and painting worked</label>
<label><input type="checkbox" name="saveWorked"> Save and resume worked</label>
<label><input type="checkbox" name="hintWorked"> Hints worked</label></fieldset>
<label>What should be changed first?</label><textarea name="comments" rows="6"></textarea>
<button>Submit Feedback</button><p id="result"></p></form>
<script>
document.getElementById('feedback').onsubmit=async event=>{{
 event.preventDefault();
 const form=new FormData(event.target);
 const payload=Object.fromEntries(form.entries());
 for(const key of ['pointerWorked','saveWorked','hintWorked']) payload[key]=form.has(key);
 const response=await fetch('/api/beta-feedback-v10',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
 document.getElementById('result').textContent=response.ok?'Thank you — your feedback was saved.':'Unable to save feedback.';
 if(response.ok) event.target.reset();
}};
</script></main></body></html>"""
