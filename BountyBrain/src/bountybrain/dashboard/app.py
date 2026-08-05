"""FastAPI dashboard: metrics, outcome logging, bounty list."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..core.models import TaskOutcome
from ..knowledge.knowledge_base import KnowledgeBase

app = FastAPI(title="BountyBrain Dashboard", version="0.1.0")
_kb = KnowledgeBase()


@app.get("/", response_class=HTMLResponse)
async def root():
    stats = _get_stats()
    phase_badge = f"Phase {stats.get('scorer_phase', 0)}"
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>BountyBrain Dashboard</title>
  <style>
    body {{ font-family: monospace; padding: 24px; background: #0d1117; color: #c9d1d9; max-width: 800px; }}
    h1 {{ color: #58a6ff; }} h2 {{ color: #79c0ff; border-bottom: 1px solid #30363d; padding-bottom: 4px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: left; }}
    th {{ background: #161b22; color: #58a6ff; }}
    .badge {{ background: #1f6feb; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
    a {{ color: #58a6ff; }}
    .stat-value {{ color: #3fb950; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>BountyBrain Dashboard <span class="badge">{phase_badge}</span></h1>

  <h2>Performance Stats</h2>
  <table>
    <tr><th>Metric</th><th>Value</th></tr>
    <tr><td>Total bounties seen</td><td class="stat-value">{stats['total_bounties']}</td></tr>
    <tr><td>Outcomes recorded</td><td class="stat-value">{stats['total_outcomes']}</td></tr>
    <tr><td>Merged</td><td class="stat-value">{stats['merged']}</td></tr>
    <tr><td>Merge rate</td><td class="stat-value">{stats['merge_rate_pct']:.1f}%</td></tr>
    <tr><td>Avg EPHH</td><td class="stat-value">${stats['avg_ephh']:.1f}/h</td></tr>
  </table>

  <h2>API Endpoints</h2>
  <ul>
    <li><a href="/api/stats">GET /api/stats</a> — JSON stats</li>
    <li><a href="/api/bounties">GET /api/bounties</a> — Recent bounties</li>
    <li><a href="/api/outcomes">GET /api/outcomes</a> — Recorded outcomes</li>
    <li><code>POST /api/outcome</code> — Log a task outcome</li>
    <li><a href="/docs">GET /docs</a> — OpenAPI docs</li>
  </ul>

  <h2>Quick Log Outcome</h2>
  <pre style="background:#161b22;padding:12px;border-radius:4px;">
curl -X POST http://localhost:8080/api/outcome \\
  -H "Content-Type: application/json" \\
  -d '{{"bounty_id":"github_123","status":"merged","payout_received":100,"human_hours_actual":1.5,"n_prompts":8,"ephh_actual":61.2}}'
  </pre>
</body>
</html>"""


@app.get("/api/stats")
async def stats():
    return _get_stats()


@app.get("/api/bounties")
async def bounties(limit: int = 50):
    return _kb.load_bounties()[-limit:]


@app.get("/api/outcomes")
async def outcomes(limit: int = 50):
    return _kb.load_outcomes()[-limit:]


@app.get("/api/outcomes/{bounty_id}")
async def get_outcome(bounty_id: str):
    outcome = _kb.get_outcome(bounty_id)
    if not outcome:
        raise HTTPException(status_code=404, detail=f"No outcome for {bounty_id}")
    return outcome


@app.post("/api/outcome")
async def record_outcome(outcome: dict):
    try:
        task_outcome = TaskOutcome(**outcome)
        _kb.record_outcome(task_outcome)
        return {"status": "ok", "bounty_id": task_outcome.bounty_id}
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


def _get_stats() -> dict:
    bounties = _kb.load_bounties()
    outcomes = _kb.load_outcomes()
    merged = [o for o in outcomes if o.get("status") == "merged"]
    ephhs = [o["ephh_actual"] for o in merged if o.get("ephh_actual", 0) > 0]
    return {
        "total_bounties": len(bounties),
        "total_outcomes": len(outcomes),
        "merged": len(merged),
        "merge_rate_pct": (len(merged) / len(outcomes) * 100) if outcomes else 0.0,
        "avg_ephh": sum(ephhs) / len(ephhs) if ephhs else 0.0,
        "scorer_phase": 0,  # TODO: read from config/scorer
    }
