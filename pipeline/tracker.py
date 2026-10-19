"""Local run tracker ("pizza tracker"): http://127.0.0.1:8766 — reads only saved progress/state files.

Shows the six stages, where the run is, records done/pending/quarantined, spend vs cap, throughput and ETA.
Refreshes every 5 s. Offline; never calls a model.
"""

import json
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common as c

PORT = 8766
STAGES = ["prepare", "enrich", "verify", "group", "rank", "memo"]


def snapshot():
    db = sqlite3.connect(f"file:{c.DB_PATH}?mode=ro", uri=True, timeout=5)
    counts = dict(db.execute("SELECT status, COUNT(*) FROM records WHERE scope_part != 'empty' GROUP BY status").fetchall())
    empties = db.execute("SELECT COUNT(*) FROM records WHERE scope_part = 'empty'").fetchone()[0]
    reasons = dict(db.execute("SELECT reason, COUNT(*) FROM records WHERE status='quarantined' GROUP BY reason").fetchall())
    prog = c.read_json(c.STATE / "progress.json") if (c.STATE / "progress.json").exists() else {}
    done = {s: (c.OUTPUTS / f"{s}_meta.json").exists() for s in ("verify", "group", "memo")}
    done["prepare"] = (c.OUTPUTS / "ingestion_report.json").exists()
    done["rank"] = (c.OUTPUTS / "ranking.csv").exists()
    total = sum(counts.values())
    done["enrich"] = total > 0 and counts.get("pending", 0) == 0
    current = next((s for s in STAGES if not done.get(s)), None)
    proj = c.read_json(c.STATE / "project_status.json") if (c.STATE / "project_status.json").exists() else {}
    return {"project": proj, "counts": counts, "empty_quarantined": empties, "quarantine_reasons": reasons, "scope_total": total,
            "progress": prog, "stages": [{"name": s, "done": bool(done.get(s)), "current": s == current} for s in STAGES]}


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Pipeline tracker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f7f6f2;--card:#fff;--ink:#1c1c1a;--mute:#6d6c66;--line:#e3e1da;--acc:#1f7a5c;--now:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mute:#9d9c95;--line:#34332f;--acc:#4fc39b;--now:#fb923c}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif}
.w{max-width:900px;margin:0 auto;padding:20px 16px}h1{font-size:20px;margin:0 0 4px}.m{color:var(--mute);font-size:13px}
.track{display:flex;gap:6px;margin:22px 0}.st{flex:1;text-align:center;font-size:13px}
.bar{height:10px;border-radius:5px;background:var(--line);margin-bottom:6px}.st.done .bar{background:var(--acc)}
.st.cur .bar{background:var(--now);animation:p 1.4s ease-in-out infinite}.st.cur{font-weight:600;color:var(--now)}
@keyframes p{50%{opacity:.45}}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.k{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.k b{display:block;font-size:24px}
.big{height:16px;background:var(--line);border-radius:8px;overflow:hidden;margin:16px 0 6px}.big div{height:100%;background:var(--acc)}
</style></head><body><div class="w">
<h1>Spotify review pipeline</h1><div class="m" id="sub">loading…</div>
<div class="track" id="track"></div>
<div class="big"><div id="fill" style="width:0"></div></div><div class="m" id="pct"></div>
<div class="g" id="kpis" style="margin-top:16px"></div>
<p class="m" id="foot"></p>
<h1 style="margin-top:26px">Project checklist</h1><div class="m" id="now"></div>
<div id="wait" style="margin:10px 0"></div><div id="steps"></div></div>
<script>
const f=n=>n==null?"—":Number(n).toLocaleString();
async function tick(){
 const d=await (await fetch("/data")).json(), p=d.progress||{}, n=d.counts;
 document.getElementById("track").innerHTML=d.stages.map(s=>`<div class="st ${s.done?"done":""} ${s.current?"cur":""}"><div class="bar"></div>${s.name}</div>`).join("");
 const done=(n.completed||0)+(n.quarantined||0), pct=d.scope_total?done/d.scope_total*100:0;
 document.getElementById("fill").style.width=pct.toFixed(2)+"%";
 document.getElementById("pct").textContent=`${pct.toFixed(1)}% of ${f(d.scope_total)} scoped reviews accounted for`;
 const live=p.status==="running";
 document.getElementById("sub").textContent=live?`Labeling now · run ${p.run_id} · ${p.workers} workers · phase ${p.phase}`:`Idle · last run ${p.run_id||"—"} ${p.status||""}${p.stop_reason?" ("+p.stop_reason+")":""}`;
 const k=[["Completed",f(n.completed)],["Pending",f(n.pending||0)],["Quarantined",f(n.quarantined||0)+" + "+d.empty_quarantined+" empty"],
  ["Spend (total)","$"+(p.spend_total_usd??0).toFixed(2)+" / $"+(p.spend_cap_usd??"—")],["Speed",f(p.records_per_minute)+" /min (paid)"],
  ["ETA",p.eta_minutes!=null&&live?`≤ ${Math.round(p.eta_minutes)} min`:"—"],["Calls this run",f(p.calls_this_run)+` (${p.failed_calls_this_run||0} failed)`],["Elapsed",(p.elapsed_minutes??0)+" min"]];
 document.getElementById("kpis").innerHTML=k.map(([a,b])=>`<div class="k"><span class="m">${a}</span><b>${b}</b></div>`).join("");
 document.getElementById("foot").textContent=`Updated ${new Date(p.updated_at||Date.now()).toLocaleTimeString()} · ETA is an upper bound: duplicate texts finish for free via exact-text reuse.`;
}
function proj(d){const p=d.project||{};const ic={done:"✅",running:"🔄",blocked:"⛔",todo:"⬜"};
 document.getElementById("now").textContent=p.now?"Claude is now: "+p.now:"";
 document.getElementById("wait").innerHTML=(p.waiting_on_brian||[]).map(w=>`<div class="k" style="border-color:var(--now);margin-bottom:6px">⏳ Waiting on you: ${w}</div>`).join("");
 document.getElementById("steps").innerHTML=(p.steps||[]).map(s=>`<div style="padding:6px 0;border-top:1px solid var(--line)">${ic[s.state]||"⬜"} ${s.name}</div>`).join("");}
const _t=tick;tick=async()=>{await _t();proj(await (await fetch("/data")).json())};
tick();setInterval(tick,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/data":
            body, ctype = json.dumps(snapshot()).encode(), "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Run tracker: http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
