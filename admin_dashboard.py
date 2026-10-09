"""Artist admin dashboard: beta tester progress and feedback in one view.

The dashboard is read-only. It aggregates the ``canvas_sessions`` rows into a
progress summary plus one row per saved painting, lists the ``beta_feedback``
submissions, and the page polls the small JSON endpoint every 30 seconds.
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


def _painting_rows(sessions: list[CanvasProgress]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
) -> dict[str, Any]:
    """Summarize saved canvas sessions and beta feedback for the admin dashboard."""
    painting_rows, progress_summary = _painting_rows(sessions)
    feedback_rows, feedback_summary = _feedback_rows(feedback or [])

    return {
        "ok": True,
        "summary": {
            **progress_summary,
            "feedbackResponses": feedback_summary["responses"],
            "averageEnjoyment": feedback_summary["averageEnjoyment"],
            "averageRecommend": feedback_summary["averageRecommend"],
            "lastFeedbackAt": feedback_summary["lastSubmittedAt"],
        },
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
.foot{color:var(--muted);font-size:13px;margin-top:14px}
@media(max-width:960px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.cards{grid-template-columns:1fr}.wrap{padding:16px}}
</style></head>
<body><div class="wrap">
<div class="top">
  <div><h1>Artist Admin — Beta Progress</h1><p class="sub">Progress of every beta tester, their active paintings, and their feedback.</p></div>
  <a href="/creator">← Back to Studio</a>
</div>
<div class="cards" id="cards"></div>
<h2>Beta feedback testers</h2>
<div class="panel"><table class="wide">
  <thead><tr><th>Date</th><th>Tester</th><th>Device</th><th>Artwork</th><th>Progress</th><th>Enjoyment</th><th>Recommend</th><th>Feature checks</th><th>Comments</th></tr></thead>
  <tbody id="feedback-rows"></tbody>
</table></div>
<h2>Saved paintings</h2>
<div class="panel"><table>
  <thead><tr><th>Tester</th><th>Artwork</th><th>Progress</th><th>Completion</th><th>Saves</th><th>Last saved</th><th>Status</th></tr></thead>
  <tbody id="rows"></tbody>
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
function statusText(status){return status === 'completed' ? 'Completed' : status === 'in-progress' ? 'In progress' : 'Not started';}
function mark(value){if(value === 1 || value === true) return '✓';if(value === 0 || value === false) return '✗';return '–';}
function checksText(item){return 'Pointer ' + mark(item.pointerWorked) + ' · Save ' + mark(item.saveWorked) + ' · Hint ' + mark(item.hintWorked);}
function barWidth(completion){return completion == null ? 0 : Math.max(2, Math.min(100, completion));}
function renderCards(summary){
  const cards = [
    ['Beta testers', summary.testers == null ? 0 : summary.testers],
    ['Saved paintings', summary.paintings == null ? 0 : summary.paintings],
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
function renderPaintings(sessions){
  const rows = sessions.map(function(item){
    const completion = item.completion;
    const painted = item.regionTotal ? item.painted + ' / ' + item.regionTotal : String(item.painted);
    return '<tr>' +
      '<td class="mono" title="' + esc(item.sessionId) + '">' + shortId(item.sessionId) + '</td>' +
      '<td class="mono">' + esc(item.artworkKey) + '</td>' +
      '<td><div class="bar"><i style="width:' + barWidth(completion) + '%"></i></div><span class="progress-note">' + esc(painted) + ' regions</span></td>' +
      '<td>' + (completion == null ? '—' : completion + '%') + '</td>' +
      '<td>' + esc(item.saves) + '</td>' +
      '<td>' + fmtTime(item.updatedAt) + '</td>' +
      '<td><span class="badge ' + esc(item.status) + '">' + statusText(item.status) + '</span></td>' +
    '</tr>';
  }).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td class="empty" colspan="7">No beta tester progress saved yet.</td></tr>';
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
  renderFeedback(data.feedback || []);
  renderPaintings(data.sessions || []);
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
