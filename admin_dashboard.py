"""Artist admin dashboard: submitted paintings, tester sessions and feedback.

Everything is read-only except the session table's "Reset progress" action,
which clears a tester's painted regions through
``/api/canvas/progress/<session>/reset``. The payload aggregates the
``canvas_sessions`` rows into a progress summary plus one row per saved session,
lists every painting the studio knows about (library rows from ``paintings``
plus the artworks that only exist as canvas sessions), lists the
``beta_feedback`` submissions, and the page polls the small JSON endpoint every
30 seconds.
"""
from __future__ import annotations

import json
from typing import Any

from canvas_progress import CanvasProgress

REFRESH_MS = 30000


def _completion_percent(painted: int, region_total: int | None) -> float | None:
    if not region_total:
        return None
    return round(painted / region_total * 100, 1)


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _artwork_name(
    artwork_key: str,
    artwork_keys: dict[str, str] | None,
    artwork_titles: dict[str, str] | None,
) -> str:
    """Name a canvas artwork by the studio artwork it belongs to."""
    studio_key = (artwork_keys or {}).get(artwork_key, artwork_key)
    return (artwork_titles or {}).get(studio_key) or artwork_key


def _artwork_rows(
    sessions: list[CanvasProgress],
    paintings: list[Any],
    artwork_keys: dict[str, str] | None = None,
    artwork_titles: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every painting the studio knows about, each carrying its session progress.

    Studio library rows (``paintings``) come first in their own order, then the
    artworks that only exist as canvas sessions — the built-in beta design and
    any upload with no painting row — most recently painted first.

    ``artwork_keys`` maps a canvas artwork key to the studio artwork it belongs
    to, so a compiled upload is one row (its painting, metadata and sessions)
    instead of two, and ``artwork_titles`` names each studio artwork.
    """
    keys = artwork_keys or {}
    titles = artwork_titles or {}
    grouped: dict[str, list[CanvasProgress]] = {}
    for session in sessions:
        grouped.setdefault(keys.get(session.artwork_key, session.artwork_key), []).append(session)

    rows: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}

    for painting in paintings:
        row = {
            "key": painting.id,
            "title": painting.title,
            "source": "library",
            "medium": painting.medium,
            "status": str(painting.status),
            "regionCount": painting.region_count,
            "colorCount": painting.color_count,
            "createdAt": painting.date_created,
            "sessions": 0,
            "paintedTotal": 0,
            "averageCompletion": None,
            "lastActivity": None,
        }
        rows.append(row)
        by_key[painting.id] = row

    for key, records in grouped.items():
        row = by_key.get(key)
        if row is None:
            row = {
                "key": key,
                "title": titles.get(key) or key,
                "source": "canvas",
                "medium": "Paint-by-number",
                "status": "not-started",
                "regionCount": max((item.region_total or 0 for item in records), default=0) or None,
                "colorCount": None,
                "createdAt": min((item.created_at for item in records), default=None),
                "sessions": 0,
                "paintedTotal": 0,
                "averageCompletion": None,
                "lastActivity": None,
            }
            rows.append(row)
            by_key[key] = row

        painted = sum(len(item.completed) for item in records)
        finished = sum(
            1 for item in records
            if item.region_total and len(item.completed) >= item.region_total
        )
        percentages = [
            value for value in (
                _completion_percent(len(item.completed), item.region_total) for item in records
            )
            if value is not None
        ]
        row["sessions"] = len(records)
        row["paintedTotal"] = painted
        row["averageCompletion"] = _average(percentages)
        row["lastActivity"] = max((item.updated_at for item in records), default=None)
        if finished:
            row["status"] = "completed"
        elif row["source"] == "canvas":
            row["status"] = "in-progress" if painted else "not-started"

    library = [row for row in rows if row["source"] == "library"]
    canvas = sorted(
        (row for row in rows if row["source"] == "canvas"),
        key=lambda row: row["lastActivity"] or "",
        reverse=True,
    )
    ordered = library + canvas

    return ordered, {"artworks": len(ordered)}


def _painting_rows(
    sessions: list[CanvasProgress],
    artwork_keys: dict[str, str] | None = None,
    artwork_titles: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    session_ids: set[str] = set()
    completion_values: list[float] = []
    completed = active = not_started = 0
    total_saves = 0

    for session in sessions:
        painted = len(session.completed)
        completion = _completion_percent(painted, session.region_total)
        if session.region_total and painted >= session.region_total:
            status = "completed"
        elif painted:
            status = "in-progress"
        else:
            status = "not-started"

        if status == "completed":
            completed += 1
        elif status == "in-progress":
            active += 1
        else:
            not_started += 1

        if completion is not None:
            completion_values.append(completion)
        session_ids.add(session.session_id)
        total_saves += session.saves

        rows.append(
            {
                "sessionId": session.session_id,
                "artworkKey": session.artwork_key,
                "artworkTitle": _artwork_name(session.artwork_key, artwork_keys, artwork_titles),
                "painted": painted,
                "regionTotal": session.region_total,
                "completion": completion,
                "saves": session.saves,
                "status": status,
                "createdAt": session.created_at,
                "updatedAt": session.updated_at,
            }
        )

    summary = {
        "testers": len(session_ids),
        "paintings": len(sessions),
        "active": active,
        "completed": completed,
        "notStarted": not_started,
        "averageCompletion": _average(completion_values),
        "totalSaves": total_saves,
        "lastActivity": max((s.updated_at for s in sessions), default=None),
    }
    return rows, summary


def _feedback_rows(feedback: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    enjoyment_values: list[float] = []
    recommend_values: list[float] = []

    for item in feedback:
        enjoyment = item.get("enjoyment")
        recommend = item.get("recommend")
        if isinstance(enjoyment, (int, float)):
            enjoyment_values.append(float(enjoyment))
        if isinstance(recommend, (int, float)):
            recommend_values.append(float(recommend))

        rows.append(
            {
                "submittedAt": item.get("submitted_at"),
                "testerName": item.get("tester_name") or "Anonymous",
                "device": item.get("device"),
                "artwork": item.get("artwork"),
                "completionStatus": item.get("completion_status"),
                "enjoyment": enjoyment,
                "easeOfUse": item.get("ease_of_use"),
                "recommend": recommend,
                "pointerWorked": item.get("pointer_worked"),
                "saveWorked": item.get("save_worked"),
                "hintWorked": item.get("hint_worked"),
                "comments": item.get("comments"),
            }
        )

    summary = {
        "responses": len(feedback),
        "averageEnjoyment": _average(enjoyment_values),
        "averageRecommend": _average(recommend_values),
        "lastSubmittedAt": max(
            (row["submittedAt"] for row in rows if row["submittedAt"]), default=None
        ),
    }
    return rows, summary


def build_admin_overview(
    sessions: list[CanvasProgress],
    feedback: list[dict[str, Any]] | None = None,
    paintings: list[Any] | None = None,
    artwork_keys: dict[str, str] | None = None,
    artwork_titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Summarize submitted paintings, saved canvas sessions and beta feedback.

    ``paintings`` are the studio library rows; ``artwork_keys`` maps a canvas
    artwork key to the studio artwork it belongs to and ``artwork_titles`` names
    those artworks. All three are optional, so a bare session/feedback summary
    still works on its own.
    """
    artwork_rows, artwork_summary = _artwork_rows(
        sessions, paintings or [], artwork_keys, artwork_titles
    )
    painting_rows, progress_summary = _painting_rows(sessions, artwork_keys, artwork_titles)
    feedback_rows, feedback_summary = _feedback_rows(feedback or [])

    return {
        "ok": True,
        "summary": {
            **progress_summary,
            **artwork_summary,
            "feedbackResponses": feedback_summary["responses"],
            "averageEnjoyment": feedback_summary["averageEnjoyment"],
            "averageRecommend": feedback_summary["averageRecommend"],
            "lastFeedbackAt": feedback_summary["lastSubmittedAt"],
        },
        "artworks": artwork_rows,
        "sessions": painting_rows,
        "feedback": feedback_rows,
    }


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Artist Admin — Beta Progress</title>
<style>
:root{--panel:#141016;--gold:#d8ad59;--purple:#8f45bd;--cream:#f4e8d6;--muted:#c2b19d;--line:#4c3827}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at 82% 3%,#32183f 0,transparent 34%),linear-gradient(180deg,#09070b,#100c11 55%,#070607);color:var(--cream);font-family:Arial,sans-serif;min-height:100vh}
a{color:var(--gold);text-decoration:none}
.wrap{max-width:1400px;margin:auto;padding:26px 30px 70px}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:22px}
h1{font-family:Georgia,serif;font-size:40px;margin:0}
h2{font-family:Georgia,serif;font-size:24px;margin:34px 0 12px;color:var(--cream)}
.sub{color:var(--muted);margin:6px 0 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(172px,1fr));gap:14px;margin:20px 0 8px}
.card{background:linear-gradient(150deg,#171218,#100d11);border:1px solid #493625;border-radius:18px;padding:18px}
.card small{display:block;color:var(--muted);text-transform:uppercase;letter-spacing:1px;font-size:11px;font-weight:800;margin-bottom:8px}
.card strong{font-family:Georgia,serif;font-size:32px}
.panel{background:#100d11;border:1px solid var(--line);border-radius:18px;padding:18px;overflow:auto}
table{width:100%;border-collapse:collapse;min-width:840px}
table.wide{min-width:1080px}
th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #2c2119;font-size:14px;vertical-align:middle}
th{color:var(--gold);text-transform:uppercase;letter-spacing:1px;font-size:11px}
.mono{font-family:Consolas,monospace}
.bar{height:9px;border-radius:999px;background:#2a2018;overflow:hidden;min-width:120px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--purple),var(--gold))}
.progress-note{color:var(--muted);font-size:12px;display:block;margin-top:5px}
.comment{color:var(--muted);max-width:320px}
.badge{border-radius:999px;padding:4px 9px;font-size:11px;font-weight:900;white-space:nowrap}
.completed{background:#dff2e4;color:#256f3d}
.in-progress{background:#efe1f7;color:#6b278e}
.not-started{background:#312a26;color:#d7c7b4}
.empty{color:var(--muted);padding:28px;text-align:center}
button.reset{padding:7px 11px;border-radius:9px;border:1px solid var(--line);background:#241a12;color:var(--cream);font-weight:800;font-size:12px;cursor:pointer;white-space:nowrap}
button.reset:hover{border-color:var(--gold);background:#2f2216}
button.reset:disabled{opacity:.5;cursor:progress}
.foot{color:var(--muted);font-size:13px;margin-top:14px}
#admin-note{min-height:18px;color:var(--gold);margin-top:10px}
@media(max-width:960px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.cards{grid-template-columns:1fr}.wrap{padding:16px}}
</style></head>
<body><div class="wrap">
<div class="top">
  <div><h1>Artist Admin — Paintings &amp; Progress</h1><p class="sub">Every painting submitted to the studio, the tester sessions painting them, and their feedback.</p></div>
  <a href="/creator">← Back to Studio</a>
</div>
<div class="cards" id="cards"></div>
<h2>Submitted paintings</h2>
<div class="panel"><table class="wide">
  <thead><tr><th>Painting</th><th>Source</th><th>Medium</th><th>Regions</th><th>Colours</th><th>Submitted</th><th>Sessions</th><th>Progress</th><th>Status</th></tr></thead>
  <tbody id="artwork-rows"></tbody>
</table></div>
<h2>Tester sessions</h2>
<div class="panel"><table class="wide">
  <thead><tr><th>Tester</th><th>Artwork</th><th>Progress</th><th>Completion</th><th>Saves</th><th>Last saved</th><th>Status</th><th>Manage</th></tr></thead>
  <tbody id="rows"></tbody>
</table></div>
<div class="foot" id="admin-note"></div>
<h2>Beta feedback testers</h2>
<div class="panel"><table class="wide">
  <thead><tr><th>Date</th><th>Tester</th><th>Device</th><th>Artwork</th><th>Progress</th><th>Enjoyment</th><th>Recommend</th><th>Feature checks</th><th>Comments</th></tr></thead>
  <tbody id="feedback-rows"></tbody>
</table></div>
<div class="foot" id="updated">Loading…</div>
</div>
<script id="initial-overview" type="application/json">__INITIAL_OVERVIEW__</script>
<script>
const REFRESH_MS = __REFRESH_MS__;
const ESCAPES = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
function esc(value){return String(value == null ? '' : value).replace(/[&<>"']/g, c => ESCAPES[c]);}
function shortId(id){const text=String(id);return esc(text.length > 12 ? text.slice(0, 8) + '…' : text);}
function fmtTime(iso){if(!iso)return '—';const date=new Date(iso);return isNaN(date) ? esc(iso) : date.toLocaleString();}
const STATUS_LABELS = {'completed':'Completed','in-progress':'In progress','in_progress':'In progress','not-started':'Not started','planned':'Planned','archived':'Archived'};
function statusText(status){return STATUS_LABELS[status] || String(status || '—').replace(/[_-]/g, ' ').replace(/^./, c => c.toUpperCase());}
function badgeClass(status){return status === 'completed' || status === 'in-progress' ? status : 'not-started';}
function mark(value){if(value === 1 || value === true) return '✓';if(value === 0 || value === false) return '✗';return '–';}
function checksText(item){return 'Pointer ' + mark(item.pointerWorked) + ' · Save ' + mark(item.saveWorked) + ' · Hint ' + mark(item.hintWorked);}
function barWidth(completion){return completion == null ? 0 : Math.max(2, Math.min(100, completion));}
function renderCards(summary){
  const cards = [
    ['Beta testers', summary.testers == null ? 0 : summary.testers],
    ['Submitted artworks', summary.artworks == null ? 0 : summary.artworks],
    ['Saved sessions', summary.paintings == null ? 0 : summary.paintings],
    ['Active paintings', summary.active == null ? 0 : summary.active],
    ['Completed', summary.completed == null ? 0 : summary.completed],
    ['Avg. completion', summary.averageCompletion == null ? '—' : summary.averageCompletion + '%'],
    ['Feedback responses', summary.feedbackResponses == null ? 0 : summary.feedbackResponses],
    ['Avg. enjoyment', summary.averageEnjoyment == null ? '—' : summary.averageEnjoyment + '/5']
  ];
  document.getElementById('cards').innerHTML = cards.map(function(card){
    return '<div class="card"><small>' + esc(card[0]) + '</small><strong>' + esc(card[1]) + '</strong></div>';
  }).join('');
}
function sourceText(source){return source === 'library' ? 'Studio library' : 'Beta canvas';}
function renderArtworks(artworks){
  const rows = artworks.map(function(item){
    const completion = item.averageCompletion;
    return '<tr>' +
      '<td><strong>' + esc(item.title) + '</strong><span class="progress-note mono">' + esc(item.key) + '</span></td>' +
      '<td>' + esc(sourceText(item.source)) + '</td>' +
      '<td>' + esc(item.medium || '—') + '</td>' +
      '<td>' + (item.regionCount == null ? '—' : esc(item.regionCount)) + '</td>' +
      '<td>' + (item.colorCount == null ? '—' : esc(item.colorCount)) + '</td>' +
      '<td>' + fmtTime(item.createdAt) + '</td>' +
      '<td>' + (item.sessions ? esc(item.sessions) + (item.sessions === 1 ? ' session' : ' sessions') : '—') + '</td>' +
      '<td><div class="bar"><i style="width:' + barWidth(completion) + '%"></i></div><span class="progress-note">' + (completion == null ? 'Not painted yet' : completion + '% painted on average') + '</span></td>' +
      '<td><span class="badge ' + esc(badgeClass(item.status)) + '">' + statusText(item.status) + '</span></td>' +
    '</tr>';
  }).join('');
  document.getElementById('artwork-rows').innerHTML = rows || '<tr><td class="empty" colspan="9">No paintings submitted yet.</td></tr>';
}
function renderPaintings(sessions){
  const rows = sessions.map(function(item){
    const completion = item.completion;
    const painted = item.regionTotal ? item.painted + ' / ' + item.regionTotal : String(item.painted);
    return '<tr>' +
      '<td class="mono" title="' + esc(item.sessionId) + '">' + shortId(item.sessionId) + '</td>' +
      '<td>' + esc(item.artworkTitle || item.artworkKey) + '<span class="progress-note mono">' + esc(item.artworkKey) + '</span></td>' +
      '<td><div class="bar"><i style="width:' + barWidth(completion) + '%"></i></div><span class="progress-note">' + esc(painted) + ' regions</span></td>' +
      '<td>' + (completion == null ? '—' : completion + '%') + '</td>' +
      '<td>' + esc(item.saves) + '</td>' +
      '<td>' + fmtTime(item.updatedAt) + '</td>' +
      '<td><span class="badge ' + esc(badgeClass(item.status)) + '">' + statusText(item.status) + '</span></td>' +
      '<td><button class="reset" type="button" data-session="' + esc(item.sessionId) + '" data-artwork="' + esc(item.artworkKey) + '">Reset progress</button></td>' +
    '</tr>';
  }).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td class="empty" colspan="8">No beta tester progress saved yet.</td></tr>';
}
function renderFeedback(feedback){
  const rows = feedback.map(function(item){
    return '<tr>' +
      '<td>' + fmtTime(item.submittedAt) + '</td>' +
      '<td>' + esc(item.testerName) + '</td>' +
      '<td>' + esc(item.device) + '</td>' +
      '<td>' + esc(item.artwork) + '</td>' +
      '<td>' + esc(item.completionStatus) + '</td>' +
      '<td>' + (item.enjoyment == null ? '—' : esc(item.enjoyment) + '/5') + '</td>' +
      '<td>' + (item.recommend == null ? '—' : esc(item.recommend) + '/10') + '</td>' +
      '<td>' + esc(checksText(item)) + '</td>' +
      '<td class="comment">' + esc(item.comments) + '</td>' +
    '</tr>';
  }).join('');
  document.getElementById('feedback-rows').innerHTML = rows || '<tr><td class="empty" colspan="9">No beta feedback submitted yet.</td></tr>';
}
function render(data){
  renderCards(data.summary || {});
  renderArtworks(data.artworks || []);
  renderPaintings(data.sessions || []);
  renderFeedback(data.feedback || []);
  document.getElementById('updated').textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · auto-refreshes every ' + (REFRESH_MS / 1000) + 's';
}
async function refresh(){
  try{
    const response = await fetch('/api/admin/overview', {cache: 'no-store'});
    if(response.ok) render(await response.json());
  }catch(error){
    /* Keep showing the last good view when a refresh fails. */
  }
}
const noteEl = document.getElementById('admin-note');
function note(text){noteEl.textContent = text;}
/* Managing a session: clearing its painted regions starts the artwork fresh for
   that tester, and the studio copy is newer, so their canvas adopts it on load. */
document.getElementById('rows').addEventListener('click', async function(event){
  const button = event.target.closest('button.reset');
  if(!button) return;
  const session = button.getAttribute('data-session');
  const artwork = button.getAttribute('data-artwork');
  const label = session.length > 8 ? session.slice(0, 8) + '…' : session;
  if(!window.confirm('Reset progress for session ' + label + ' on ' + artwork + '? The tester starts that painting again from an empty canvas.')) return;
  button.disabled = true;
  note('Resetting session ' + label + '…');
  try{
    const response = await fetch('/api/canvas/progress/' + encodeURIComponent(session) + '/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({artworkKey: artwork})
    });
    if(!response.ok) throw new Error('Reset rejected');
    const data = await response.json();
    note('Cleared ' + data.cleared + ' saved record(s) for session ' + label + '.');
    await refresh();
  }catch(error){
    note('Could not reset session ' + label + '. Try again.');
    button.disabled = false;
  }
});
try{render(JSON.parse(document.getElementById('initial-overview').textContent));}
catch(error){refresh();}
setInterval(refresh, REFRESH_MS);
</script>
</body></html>"""


def render_admin_dashboard(overview: dict[str, Any]) -> str:
    """The dashboard page, with the first payload inlined so it paints at once."""
    initial = json.dumps(overview, ensure_ascii=False).replace("</", "<\\/")
    return (
        _PAGE.replace("__INITIAL_OVERVIEW__", initial)
        .replace("__REFRESH_MS__", str(REFRESH_MS))
    )
