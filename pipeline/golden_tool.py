"""Local labeling page for the golden 50 (human labels only; no model is involved).

Run:  python -m pipeline label-golden     -> opens http://127.0.0.1:8765
Every change autosaves to evals/golden_human_labels.json. Close the terminal when done.
"""

import csv
import html
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common as c

GOLDEN_CSV = c.DATASET / "golden_50_to_label.csv"
LABELS = c.ROOT / "evals" / "golden_human_labels.json"
PORT = 8765


def load_reviews():
    with GOLDEN_CSV.open(encoding="utf-8", newline="") as f:
        return [{k: r[k] for k in c.FIELDS} for r in csv.DictReader(f)]


def load_labels():
    return c.read_json(LABELS) if LABELS.exists() else {}


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Golden 50 labeling</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fafaf8;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--acc:#1f6f5c;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--mute:#9b9a94;--line:#33332f;--acc:#5cc0a3;--warn:#f0a35c}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:20px;max-width:1200px;margin:0 auto;padding:16px}
@media (max-width:900px){.wrap{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:12px}
.review{font-size:19px;line-height:1.5;white-space:pre-wrap;user-select:text}
.meta{color:var(--mute);font-size:13px;margin-top:8px}
.row{margin:12px 0}.row b{display:block;margin-bottom:6px}
.opts{display:flex;flex-wrap:wrap;gap:6px}
.opts label{border:1px solid var(--line);border-radius:6px;padding:5px 9px;cursor:pointer}
.opts input{display:none}.opts input:checked+span{color:var(--acc);font-weight:600}
.opts label:has(input:checked){border-color:var(--acc)}
input[type=text],textarea{width:100%;box-sizing:border-box;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:7px;font:inherit}
.bad{color:var(--warn);font-size:13px}.ok{color:var(--acc);font-size:13px}
.nav{display:flex;gap:8px;align-items:center;justify-content:space-between}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
button.alt{background:transparent;color:var(--ink);border:1px solid var(--line)}
.dots{display:flex;flex-wrap:wrap;gap:4px;margin-top:10px}.dots span{width:14px;height:14px;border-radius:3px;background:var(--line);cursor:pointer}
.dots span.done{background:var(--acc)}.dots span.cur{outline:2px solid var(--ink)}
.defs{font-size:13px;max-height:calc(100vh - 40px);overflow:auto;position:sticky;top:16px}
.defs table{border-collapse:collapse;width:100%}.defs td,.defs th{border-top:1px solid var(--line);padding:4px;vertical-align:top;text-align:left}
</style></head><body><div class="wrap"><main>
<div class="card nav"><div><b id="pos"></b> <span class="meta" id="saved"></span></div>
<div><button class="alt" onclick="go(-1)">← Prev</button> <button onclick="go(1)">Next →</button></div></div>
<div class="card"><div class="review" id="text"></div><div class="meta" id="meta"></div>
<div class="meta">Tip: select words in the review, then click <i>Use selection as quote</i>.</div></div>
<div class="card" id="form"></div>
<div class="card"><div class="dots" id="dots"></div></div>
</main><aside class="card defs">__DEFS__</aside></div>
<script>
const R=__REVIEWS__; let L=__LABELS__; let i=0;
const TOPICS=["access","usability","playback","downloads","catalog","billing","support","other"];
const INTENTS=["cancellation","complaint","request","praise","unclear"];
function radios(name,opts,val){return `<div class="opts">${opts.map(o=>`<label><input type="radio" name="${name}" value="${o}" ${(String(val)===String(o)||(val!==undefined&&val!==null&&val!==""&&!isNaN(o)&&Number(val)===Number(o)))?"checked":""}><span>${o}</span></label>`).join("")}</div>`}
function done(l){return l&&l.topic&&l.intent&&l.severity&&l.sentiment!==undefined&&l.sentiment!==""&&l.evidence_quote&&R.find(r=>r.review_id===l.review_id).review_text.includes(l.evidence_quote)}
function render(){
 const r=R[i], l=L[r.review_id]||{review_id:r.review_id};
 document.getElementById("pos").textContent=`Review ${i+1} of ${R.length} · ${Object.values(L).filter(done).length} complete`;
 document.getElementById("text").textContent=r.review_text;
 document.getElementById("meta").textContent=`★ ${r.review_rating} · ${r.review_likes} likes · app ${r.app_version||"—"} · ${r.review_timestamp}  (metadata: don't label from stars)`;
 const q=l.evidence_quote||"", qok=q&&r.review_text.includes(q);
 document.getElementById("form").innerHTML=`
 <div class="row"><b>Topic</b>${radios("topic",TOPICS,l.topic)}</div>
 <div class="row"><b>Intent</b> <span class="meta">(precedence: cancellation → complaint → request → praise → unclear)</span>${radios("intent",INTENTS,l.intent)}</div>
 <div class="row"><b>Severity</b>${radios("severity",[1,2,3,4,5],l.severity)}</div>
 <div class="row"><b>Sentiment</b> <span class="meta">−1 very negative · 0 neutral/mixed · +1 very positive</span>${radios("sentiment",["-1.0","-0.6","-0.3","0.0","0.3","0.6","1.0"],l.sentiment)}</div>
 <div class="row"><b>Evidence quote</b> <span class="${qok?"ok":"bad"}">${q?(qok?"✓ exact substring":"✗ not an exact substring"):"required"}</span>
  <input type="text" id="quote" value="${q.replace(/"/g,"&quot;")}"> <button class="alt" type="button" onclick="useSel()">Use selection as quote</button> <button class="alt" type="button" onclick="useAll()">Use whole review</button></div>
 <div class="row"><b>Entities</b> <span class="meta">comma-separated features named in the text; blank if none</span><input type="text" id="entities" value="${(l.entities||[]).join(", ").replace(/"/g,"&quot;")}"></div>
 <div class="row"><label><input type="checkbox" id="nr" ${l.needs_review?"checked":""}> needs_review (ambiguous / missing context)</label></div>
 <div class="row"><b>Also acceptable</b> <span class="meta">optional, if ambiguous: other defensible labels, e.g. "topic=billing; severity=3"</span><input type="text" id="alt" value="${(l.also_acceptable||"").replace(/"/g,"&quot;")}"></div>
 <div class="row"><b>Notes</b><textarea id="notes" rows="2">${l.notes||""}</textarea></div>`;
 document.querySelectorAll("#form input,#form textarea").forEach(e=>e.addEventListener("change",save));
 document.getElementById("dots").innerHTML=R.map((x,k)=>`<span title="${k+1}" class="${done(L[x.review_id])?"done":""} ${k===i?"cur":""}" onclick="i=${k};render()"></span>`).join("");
}
function val(n){const e=document.querySelector(`input[name=${n}]:checked`);return e?e.value:undefined}
function save(){
 const r=R[i];
 L[r.review_id]={review_id:r.review_id,topic:val("topic"),intent:val("intent"),severity:val("severity")?parseInt(val("severity")):undefined,
  sentiment:val("sentiment")!==undefined?parseFloat(val("sentiment")):undefined,evidence_quote:document.getElementById("quote").value,
  entities:document.getElementById("entities").value.split(",").map(s=>s.trim()).filter(Boolean),needs_review:document.getElementById("nr").checked,
  also_acceptable:document.getElementById("alt").value.trim(),notes:document.getElementById("notes").value.trim(),labeled_at:new Date().toISOString()};
 fetch("/save",{method:"POST",body:JSON.stringify(L)}).then(x=>x.ok?x.text():Promise.reject()).then(t=>{document.getElementById("saved").textContent="saved "+new Date().toLocaleTimeString()}).catch(()=>{document.getElementById("saved").textContent="⚠ save failed"});
 render();
}
function useSel(){const s=String(window.getSelection()).trim(); if(s){document.getElementById("quote").value=s; save();}}
function useAll(){document.getElementById("quote").value=R[i].review_text; save();}
function go(d){i=Math.max(0,Math.min(R.length-1,i+d));render();window.scrollTo(0,0)}
render();
</script></body></html>"""


def definitions_html():
    """Render labels/definitions.md as simple HTML for the side panel (tables + headings + lists)."""
    import re
    out = []
    for line in (c.ROOT / "labels" / "definitions.md").read_text(encoding="utf-8").splitlines():
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(line).replace("`", ""))
        if line.startswith("|"):
            cells = [x.strip() for x in s.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            out.append("<tr>" + "".join(f"<td>{x}</td>" for x in cells) + "</tr>")
            continue
        if out and out[-1].startswith("<tr>") and not line.startswith("|"):
            pass
        if line.startswith("#"):
            out.append(f"<h3>{s.lstrip('#').strip()}</h3>")
        elif s.strip():
            out.append(f"<p>{s}</p>")
    body = "\n".join(out)
    # wrap consecutive rows into tables
    import re
    return re.sub(r"((?:<tr>.*?</tr>\n?)+)", r"<table>\1</table>", body)


ADJ = c.ROOT / "evals" / "golden_adjudication.json"
ADJ_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Golden adjudication</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:#fafaf8;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--acc:#1f6f5c}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--mute:#9b9a94;--line:#33332f;--acc:#5cc0a3}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif}.w{max-width:900px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}.m{color:var(--mute);font-size:13px}
.t{font-size:17px;white-space:pre-wrap;margin:6px 0 10px}table{border-collapse:collapse}td{padding:3px 10px 3px 0}
.opts{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.opts label{border:1px solid var(--line);border-radius:6px;padding:5px 9px;cursor:pointer}
.opts label:has(input:checked){border-color:var(--acc);color:var(--acc);font-weight:600}.opts input{display:none}
input[type=text]{width:100%;box-sizing:border-box;margin-top:8px;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
</style></head><body><div class="w"><h2>Adjudicate disagreements</h2>
<p class="m">Your original labels stay on file as the official blind result. For each disagreement, record which label you now believe fits the shared definitions. This is post-hoc (you have seen the model's answer) and is reported separately. Autosaves.</p>
<div id="list"></div></div><script>
const D=__DIS__; let A=__ADJ__;
const OPTS=[["human_error","My label was wrong (model fits the definitions)"],["model_error","Model was wrong (my label fits)"],["both_ok","Both acceptable / genuinely ambiguous"]];
function render(){document.getElementById("list").innerHTML=D.map((d,i)=>{const a=A[d.review_id]||{};return `<div class="card"><div class="m">${i+1} of ${D.length} · ${a.verdict?"✓ decided":"not decided"}</div>
<div class="t">${d.text.replace(/</g,"&lt;")}</div><table><tr><td class="m">You</td><td><b>${d.h}</b></td></tr><tr><td class="m">Model</td><td><b>${d.p}</b></td></tr></table>
<div class="opts">${OPTS.map(([v,l])=>`<label><input type="radio" name="v${i}" value="${v}" ${a.verdict===v?"checked":""} onchange="save('${d.review_id}',this.value,null)"><span>${l}</span></label>`).join("")}</div>
<input type="text" placeholder="optional note (why)" value="${(a.note||"").replace(/"/g,"&quot;")}" onchange="save('${d.review_id}',null,this.value)"></div>`}).join("")}
function save(id,v,note){A[id]=A[id]||{};if(v)A[id].verdict=v;if(note!==null)A[id].note=note;A[id].at=new Date().toISOString();
fetch("/save-adj",{method:"POST",body:JSON.stringify(A)});render()}
render();</script></body></html>"""


def disagreements():
    ev = c.read_json(c.ROOT / "evals" / "golden_eval.json")
    fmt = lambda x: f"{x['topic']} / {x['intent']} / severity {x['severity']}" if x else "no prediction"
    return [{"review_id": d["review_id"], "text": d["text"], "h": fmt(d["human"]), "p": fmt(d["prediction"])}
            for d in ev["disagreements"]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/adjudicate"):
            page = (ADJ_PAGE.replace("__DIS__", json.dumps(disagreements(), ensure_ascii=False))
                    .replace("__ADJ__", json.dumps(c.read_json(ADJ) if ADJ.exists() else {}, ensure_ascii=False)))
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        page = (PAGE.replace("__REVIEWS__", json.dumps(load_reviews(), ensure_ascii=False))
                    .replace("__LABELS__", json.dumps(load_labels(), ensure_ascii=False))
                    .replace("__DEFS__", definitions_html()))
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/save-adj":
            c.write_json(ADJ, json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
            self.send_response(200)
            self.end_headers()
            return
        if self.path != "/save":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        labels = json.loads(body)
        c.write_json(LABELS, labels)  # atomic
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def serve(open_browser=True):
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Golden labeling page: {url}  (autosaves to {LABELS.relative_to(c.ROOT)}). Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def export():
    """Write evals/golden_human_labels.csv in the course template's columns; report what's missing."""
    reviews, labels = load_reviews(), load_labels()
    rows, missing = [], []
    for r in reviews:
        l = labels.get(r["review_id"], {})
        ok = all(l.get(k) not in (None, "") for k in ("topic", "intent", "severity", "sentiment", "evidence_quote")) \
            and l["evidence_quote"] in r["review_text"]
        if not ok:
            missing.append(r["review_id"])
        rows.append({**r, "topic": l.get("topic", ""), "intent": l.get("intent", ""), "sentiment": l.get("sentiment", ""),
                     "severity": l.get("severity", ""), "entities": json.dumps(l.get("entities", []), ensure_ascii=False),
                     "evidence_quote": l.get("evidence_quote", ""), "needs_review": l.get("needs_review", ""),
                     "also_acceptable": l.get("also_acceptable", ""), "notes": l.get("notes", "")})
    out = c.ROOT / "evals" / "golden_human_labels.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {out.relative_to(c.ROOT)}: {len(rows) - len(missing)}/{len(rows)} complete")
    return missing
