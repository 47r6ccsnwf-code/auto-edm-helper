#!/usr/bin/env python3
"""
Suntec Cold-Email Mailer — local web UI (Flask).

Run:
  python webapp.py
  -> open http://127.0.0.1:5000

Features:
  - Dashboard: sent / bounced / failed / today counts
  - Leads view: filtered list with language + intent score
  - Template preview: render a real personalized email for any lead
  - Send: launches a throttled background campaign (dry-run or live),
          with live progress polled by the page.
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402

try:
    from flask import Flask, jsonify, request, render_template_string
except ImportError:
    raise SystemExit("Missing dependency: pip install flask")

app = Flask(__name__)

# ---- shared campaign state (single local user) --------------------------- #
STATE = {
    "running": False,
    "dry_run": True,
    "log": [],
    "summary": {},
}
LOCK = threading.Lock()


def get_cfg():
    return engine.load_config(os.environ.get("MAILER_CONFIG"))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.route("/api/stats")
def api_stats():
    cfg = get_cfg()
    db_path = engine._resolve(HERE, cfg["tracking"]["db_path"])
    if not os.path.exists(db_path):
        return jsonify({"sent": 0, "bounced": 0, "failed": 0, "today_total": 0,
                        "recent": []})
    t = engine.Tracker(db_path)
    s = t.stats()
    s["recent"] = t.recent(20)
    return jsonify(s)


@app.route("/api/inbox")
def api_inbox():
    cfg = get_cfg()
    limit = int(request.args.get("limit", 10))
    emails = engine.get_recent_emails(cfg, limit=limit)
    return jsonify({"emails": emails})


@app.route("/api/leads")
def api_leads():
    cfg = get_cfg()
    leads = engine.load_leads(cfg)
    out = []
    for ld in leads[:500]:
        out.append({
            "company": ld.get("Company", ""),
            "country": ld.get("Country", ""),
            "email": ld.get("Email", ""),
            "tier": ld.get("Tier", ""),
            "score": ld.get("IntentScore", ""),
            "lang": engine.pick_lang(cfg, ld.get("Country", "")),
            "product": ld.get("ProductMatch", ""),
        })
    return jsonify({"count": len(leads), "shown": len(out), "leads": out})


@app.route("/api/preview")
def api_preview():
    """Render personalized email for the lead at given index."""
    cfg = get_cfg()
    leads = engine.load_leads(cfg)
    idx = int(request.args.get("i", 0))
    if not leads:
        return jsonify({"error": "no leads"}), 404
    idx = max(0, min(idx, len(leads) - 1))
    ld = leads[idx]
    sender = cfg["sender"]
    tdir = engine._resolve(HERE, cfg["templates"]["dir"])
    lang = engine.pick_lang(cfg, ld.get("Country", ""))
    tpl = engine.load_template(tdir, lang, cfg["templates"].get("default_lang", "en"))
    return jsonify({
        "index": idx,
        "total": len(leads),
        "from_email": cfg["accounts"][0]["from_email"],
        "to": ld.get("Email", ""),
        "company": ld.get("Company", ""),
        "country": ld.get("Country", ""),
        "lang": lang,
        "subject": engine.personalize(tpl["subject"], ld, sender),
        "body": engine.personalize(tpl["body"], ld, sender),
    })


def _run_campaign_thread(cfg, dry_run, limit):
    with LOCK:
        STATE["running"] = True
        STATE["dry_run"] = dry_run
        STATE["log"] = []
        STATE["summary"] = {}

    def progress(line):
        with LOCK:
            STATE["log"].append(line)
            STATE["log"] = STATE["log"][-300:]

    try:
        res = engine.run_campaign(cfg, dry_run=dry_run, limit=limit,
                                  progress=progress)
        with LOCK:
            STATE["summary"] = {
                "attempted": res.attempted, "sent": res.sent,
                "failed": res.failed, "skipped": res.skipped,
                "stopped_reason": res.stopped_reason,
            }
    finally:
        with LOCK:
            STATE["running"] = False


@app.route("/api/send", methods=["POST"])
def api_send():
    if STATE["running"]:
        return jsonify({"error": "a campaign is already running"}), 409
    data = request.get_json(force=True, silent=True) or {}
    dry_run = bool(data.get("dry_run", True))
    limit = data.get("limit")
    limit = int(limit) if limit else None
    cfg = get_cfg()
    th = threading.Thread(target=_run_campaign_thread,
                          args=(cfg, dry_run, limit), daemon=True)
    th.start()
    return jsonify({"started": True, "dry_run": dry_run, "limit": limit})


@app.route("/api/progress")
def api_progress():
    with LOCK:
        return jsonify({
            "running": STATE["running"],
            "dry_run": STATE["dry_run"],
            "log": STATE["log"][-60:],
            "summary": STATE["summary"],
        })


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
PAGE = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Suntec Mailer — Swiss Dashboard</title>
<style>
:root {
  --bg: #f8f9fa;
  --ink: #1a1a1a;
  --accent: #0050ff; /* Klein Blue */
  --muted: #6c757d;
  --border: #e1e4e8;
  --card-bg: #ffffff;
  --ok: #28a745;
  --bad: #dc3545;
  --warn: #ffc107;
}

body {
  margin: 0;
  font-family: 'Inter', -apple-system, sans-serif;
  background: var(--bg);
  color: var(--ink);
  line-height: 1.2;
}

header {
  border-bottom: 2px solid var(--ink);
  padding: 40px 60px;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  background: #fff;
}

header h1 {
  margin: 0;
  font-size: 48px;
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: -2px;
}

header .meta {
  text-align: right;
  font-weight: 600;
}

.dashboard {
  padding: 20px 60px;
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 20px;
}

.card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  padding: 24px;
  display: flex;
  flex-direction: column;
}

.card h2 {
  margin: 0 0 20px 0;
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 2px;
  font-weight: 800;
  border-bottom: 1px solid var(--ink);
  padding-bottom: 8px;
}

/* Grid layout for cards */
.col-12 { grid-column: span 12; }
.col-8  { grid-column: span 8; }
.col-4  { grid-column: span 4; }
.col-3  { grid-column: span 3; }

.stat-n {
  font-size: 64px;
  font-weight: 900;
  margin: 10px 0;
}

.stat-l {
  font-size: 12px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
}

.stat-sent { color: var(--ok); }
.stat-failed { color: var(--bad); }

table {
  width: 100%;
  border-collapse: collapse;
}

th {
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  color: var(--muted);
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}

td {
  padding: 12px 0;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
}

.badge {
  padding: 2px 6px;
  border-radius: 0;
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
}

.b-sent { background: var(--ok); color: #fff; }
.b-failed { background: var(--bad); color: #fff; }
.b-bounced { background: var(--warn); color: #000; }

.btn {
  background: var(--ink);
  color: #fff;
  border: 0;
  padding: 12px 24px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 1px;
  cursor: pointer;
  margin-right: 10px;
}

.btn:hover { background: var(--accent); }
.btn-warn { background: var(--bad); }

pre {
  background: #f1f3f5;
  padding: 15px;
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
  overflow: auto;
  border-left: 4px solid var(--accent);
}

.email-item {
  border-bottom: 1px solid var(--border);
  padding: 15px 0;
}
.email-item:last-child { border: 0; }
.email-from { font-weight: 800; font-size: 14px; }
.email-subj { font-weight: 400; color: var(--accent); margin-top: 4px; }
.email-date { font-size: 11px; color: var(--muted); }
.email-snippet { font-size: 12px; margin-top: 8px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

input {
  border: 1px solid var(--border);
  padding: 8px;
  font-family: inherit;
}

/* Animations */
@keyframes blink { 50% { opacity: 0; } }
.live-dot {
  width: 10px; height: 10px; background: var(--bad); border-radius: 50%;
  display: inline-block; margin-right: 5px;
  animation: blink 1s infinite;
}
</style></head><body>

<header>
  <div>
    <h1>Suntec</h1>
    <div style="font-weight:600;letter-spacing:2px">COLD-EMAIL SYSTEM V2</div>
  </div>
  <div class="meta">
    <div id="clock">00:00:00</div>
    <div id="leadcount">0 LEADS MATCHED</div>
  </div>
</header>

<div class="dashboard">
  <!-- Stats row -->
  <div class="card col-3">
    <div class="stat-l">Total Sent</div>
    <div class="stat-n stat-sent" id="s-sent">0</div>
  </div>
  <div class="card col-3">
    <div class="stat-l">Failed / Bounced</div>
    <div class="stat-n stat-failed" id="s-failed">0</div>
  </div>
  <div class="card col-3">
    <div class="stat-l">Today Vol</div>
    <div class="stat-n" id="s-today">0</div>
  </div>
  <div class="card col-3">
    <div class="stat-l">Queue Status</div>
    <div id="run-state" style="font-size:24px;font-weight:800;margin:20px 0;">STANDBY</div>
  </div>

  <!-- Left Col -->
  <div class="col-8" style="display:flex;flex-direction:column;gap:20px;">
    <div class="card">
      <h2>🚀 Campaign Control</h2>
      <div style="margin-bottom:20px">
        Limit: <input id="limit" type="number" placeholder="No limit" style="width:100px">
      </div>
      <div>
        <button class="btn" onclick="run(true)">Dry Run</button>
        <button class="btn btn-warn" onclick="run(false)">Execute Live</button>
      </div>
      <pre id="runlog" style="margin-top:20px;height:200px">System ready.</pre>
    </div>

    <div class="card">
      <h2>✉️ Content Preview</h2>
      <div style="display:flex;gap:10px;margin-bottom:15px">
        <button class="btn" style="padding:5px 10px" onclick="nav(-1)">PREV</button>
        <button class="btn" style="padding:5px 10px" onclick="nav(1)">NEXT</button>
        <span id="pv-pos" style="font-weight:800;align-self:center">0 / 0</span>
      </div>
      <div style="font-size:13px">
        <div><b>FROM:</b> <span id="pv-from"></span></div>
        <div><b>TO:</b> <span id="pv-to" style="font-weight:800"></span></div>
        <div><b>SUBJ:</b> <span id="pv-subj" style="color:var(--accent);font-weight:800"></span></div>
      </div>
      <pre id="pv-body" style="margin-top:15px;"></pre>
    </div>

    <div class="card">
      <h2>📋 Active Lead List</h2>
      <div style="max-height:400px;overflow:auto">
        <table>
          <thead><tr><th>#</th><th>Company</th><th>Country</th><th>Score</th></tr></thead>
          <tbody id="leadrows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Right Col -->
  <div class="col-4" style="display:flex;flex-direction:column;gap:20px;">
    <div class="card" style="border:2px solid var(--accent)">
      <h2>📥 Inbox / Replies</h2>
      <div id="inbox-list" style="max-height:500px;overflow:auto">
        <div style="padding:20px;text-align:center;color:var(--muted)">Fetching replies...</div>
      </div>
      <button class="btn" style="margin-top:15px;width:100%" onclick="loadInbox()">Refresh Inbox</button>
    </div>

    <div class="card">
      <h2>🕓 Logs / Outbound</h2>
      <div style="max-height:400px;overflow:auto">
        <table>
          <thead><tr><th>Time</th><th>Target</th><th>Status</th></tr></thead>
          <tbody id="recentrows"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<script>
let pvIndex=0, pvTotal=0;
const j = (u,o) => fetch(u,o).then(r=>r.json());

function updateClock(){
  document.getElementById('clock').textContent = new Date().toLocaleTimeString('en-GB');
}

async function loadStats(){
  const s = await j('/api/stats');
  document.getElementById('s-sent').textContent = s.sent||0;
  document.getElementById('s-failed').textContent = (s.failed||0) + (s.bounced||0);
  document.getElementById('s-today').textContent = s.today_total||0;
  
  document.getElementById('recentrows').innerHTML = (s.recent||[]).map(r => `
    <tr>
      <td><small>${(r.sent_at||'').split('T')[1]}</small></td>
      <td>${r.company || r.email}</td>
      <td><span class="badge b-${r.status}">${r.status}</span></td>
    </tr>
  `).join('');
}

async function loadInbox(){
  const d = await j('/api/inbox?limit=15');
  const box = document.getElementById('inbox-list');
  if(!d.emails || d.emails.length === 0) {
    box.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">No replies found.</div>';
    return;
  }
  box.innerHTML = d.emails.map(e => `
    <div class="email-item">
      <div class="email-date">${e.date}</div>
      <div class="email-from">${e.from}</div>
      <div class="email-subj">${e.subject}</div>
      <div class="email-snippet">${e.snippet}...</div>
    </div>
  `).join('');
}

async function loadLeads(){
  const d = await j('/api/leads');
  document.getElementById('leadcount').textContent = d.count + ' LEADS LOADED';
  document.getElementById('leadrows').innerHTML = d.leads.map((l,i) => `
    <tr onclick="goto(${i})" style="cursor:pointer">
      <td>${i+1}</td>
      <td>${l.company}</td>
      <td>${l.country}</td>
      <td><span style="font-weight:900">${l.score}</span></td>
    </tr>
  `).join('');
  pv_total = d.total;
  document.getElementById('pv-pos').textContent = (p.index+1) + ' / ' + p.total;
  document.getElementById('pv-from').textContent = p.from_email;
  document.getElementById('pv-to').textContent = p.to;

  document.getElementById('pv-subj').textContent = p.subject;
  document.getElementById('pv-body').textContent = p.body;
}

function nav(d){
  let n = pvIndex+d;
  if(n < 0) n = 0; if(n >= pvTotal) n = pvTotal-1;
  goto(n);
}

async function run(dry){
  const lim = document.getElementById('limit').value;
  const r = await j('/api/send', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dry_run:dry, limit:lim?parseInt(lim):null})
  });
  if(r.error){ alert(r.error); return; }
  poll();
}

async function poll(){
  const p = await j('/api/progress');
  const state = document.getElementById('run-state');
  if(p.running) {
    state.innerHTML = '<span class="live-dot"></span> ' + (p.dry_run ? 'DRY RUN' : 'EXECUTING');
    state.style.color = p.dry_run ? 'var(--accent)' : 'var(--bad)';
  } else {
    state.textContent = 'STANDBY';
    state.style.color = 'var(--ink)';
  }

  document.getElementById('runlog').textContent = (p.log||[]).map(l => {
    if(l.msg==='SENT') return `[OK] SENT TO ${l.company}`;
    if(l.msg==='pause') return `     PAUSE ${l.seconds}S`;
    if(l.msg==='FAILED') return `[ERR] ${l.email}`;
    return l.msg;
  }).join('\\n');

  if(p.running) setTimeout(poll, 2000);
  else loadStats();
}

// Init
setInterval(updateClock, 1000);
loadStats();
loadLeads();
loadInbox();
setInterval(loadStats, 10000);
setInterval(loadInbox, 60000);
</script></body></html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Suntec Mailer UI -> http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
