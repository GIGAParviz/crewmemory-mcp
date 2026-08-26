from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import ConfigError, load_config, Config
from .store import Store, StoreError

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crew Memory</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#21262d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:5}
header h1{font-size:16px;margin:0}
header .sub{color:var(--muted);font-size:12px}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;border:1px solid var(--border);background:var(--card)}
nav{display:flex;gap:4px;padding:10px 22px;border-bottom:1px solid var(--border);flex-wrap:wrap}
nav button{background:none;border:1px solid transparent;color:var(--muted);padding:6px 12px;border-radius:8px;cursor:pointer;font-size:13px}
nav button.active{color:var(--text);background:var(--card);border-color:var(--border)}
main{max-width:1100px;margin:0 auto;padding:20px 22px 60px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.card h3{margin:0 0 6px;font-size:14px}
.muted{color:var(--muted);font-size:12px}
.row{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap}
.bar{height:6px;background:#21262d;border-radius:4px;margin:8px 0;overflow:hidden}
.bar i{display:block;height:100%;background:var(--green);border-radius:4px}
.blocker{color:var(--red);font-size:12px;margin-top:4px}
.stale{color:var(--yellow)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.chip{cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--muted);padding:4px 10px;border-radius:12px;font-size:12px}
.chip.on{color:#000;background:var(--accent);border-color:var(--accent)}
input[type=search]{flex:1;min-width:200px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px}
.entry{border-left:3px solid var(--border);padding-left:10px;margin:10px 0}
.entry h4{margin:0 0 2px;font-size:13.5px;cursor:pointer}
pre{white-space:pre-wrap;word-break:break-word;background:#0a0d12;border:1px solid var(--border);border-radius:8px;padding:10px;font-size:12px;display:none;margin-top:6px}
.tag{display:inline-block;color:var(--accent);font-size:11px;margin-right:6px}
.t-note{border-color:#8b949e}.t-decision{border-color:var(--yellow)}.t-solution{border-color:var(--green)}.t-gotcha{border-color:var(--red)}.t-pattern{border-color:#bc8cff}.t-handoff{border-color:var(--accent)}
.timeline div{padding:7px 0;border-bottom:1px solid var(--border);font-size:13px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 18px;font-size:13px}
.refresh{margin-left:auto;color:var(--muted);font-size:11px}
.empty{color:var(--muted);text-align:center;padding:40px 0}
</style>
</head>
<body>
<header>
  <h1>Crew Memory</h1>
  <span class="badge" id="repo"></span>
  <span class="sub" id="meta"></span>
  <span class="refresh" id="refreshed"></span>
</header>
<nav id="tabs">
  <button data-t="now" class="active">Team now</button>
  <button data-t="activity">Activity</button>
  <button data-t="memories">Memories</button>
  <button data-t="profiles">Profiles</button>
  <button data-t="overview">Overview</button>
</nav>
<main id="main"><div class="empty">Loading…</div></main>
<script>
let DATA=null,TAB='now',FILTER='',KIND=null,AUTHOR=null;
const $=s=>document.querySelector(s);
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{TAB=b.dataset.t;document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('active',x===b));render()});
function esc(s){return (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function ago(iso){if(!iso)return '';const d=(Date.now()-new Date(iso.replace(' ','T')+'Z').getTime())/1000;if(d<60)return 'just now';if(d<3600)return Math.floor(d/60)+'m ago';if(d<86400)return Math.floor(d/3600)+'h ago';return Math.floor(d/86400)+'d ago'}
function load(){fetch('/api/all').then(r=>r.json()).then(j=>{DATA=j;$('#repo').textContent=j.repo_name;j.meta&&( $('#meta').textContent=`project ${j.meta.project||'-'} @ ${j.meta.branch||'-'} · you: ${j.meta.user}`);$('#refreshed').textContent='updated '+new Date().toLocaleTimeString();render()}).catch(()=>{$('#refreshed').textContent='offline'})}
function render(){
 const m=$('#main');
 if(TAB==='now'){
   const ss=[...(DATA.team.statuses||[]),...(DATA.personal.statuses||[])].sort((a,b)=>(b.updated||'').localeCompare(a.updated||''));
   m.innerHTML='<div class="grid">'+(ss.length?ss.map(s=>{
     const p=s.progress!=null?Math.max(0,Math.min(100,s.progress)):null;
     return `<div class="card"><div class="row"><h3>${esc(s.user)}</h3><span class="badge ${s.stale?'stale':''}">${s.stale?'stale':ago(s.updated)}</span></div>
     <div>${esc(s.task||s.message||'')}</div>
     ${p!=null?`<div class="bar"><i style="width:${p}%"></i></div><div class="muted">${p}% done${s.project?` · ${esc(s.project)}@${esc(s.branch||'')}`:''}</div>`:`<div class="muted">${s.project?esc(s.project)+'@'+esc(s.branch||''):''}</div>`}
     ${(s.blockers||[]).map(b=>`<div class="blocker">⛔ ${esc(b)}</div>`).join('')}</div>`;
   }).join(''):'<div class="empty">No statuses yet — agents announce via update_status()</div>')+'</div>';
 }
 if(TAB==='activity'){
   const ev=[...(DATA.team.activity||[]),...(DATA.personal.activity||[])].sort((a,b)=>(b.ts||'').localeCompare(a.ts||'')).slice(0,300);
   const lbl={note:'saved note',decision:'logged decision',solution:'logged solution',gotcha:'saved gotcha',pattern:'saved pattern',handoff:'wrote handoff',status:'status',deleted:'deleted',lifecycle:'lifecycle'};
   m.innerHTML='<div class="card timeline">'+(ev.length?ev.map(e=>`<div><b>${esc(e.user)}</b> <span class="muted">${lbl[e.action]||e.action}</span> — ${esc(e.detail||'')} <span class="muted" style="float:right">${ago(e.ts)}</span></div>`).join(''):'<div class="empty">No activity yet</div>')+'</div>';
 }
 if(TAB==='memories'){
   const all=[...(DATA.team.entries||[]),...(DATA.personal.entries||[])];
   const kinds=[...new Set(all.map(e=>e.type))];
   const authors=[...new Set(all.map(e=>e.author))];
   let list=all;
   if(KIND)list=list.filter(e=>e.type===KIND);
   if(AUTHOR)list=list.filter(e=>e.author===AUTHOR);
   if(FILTER){const f=FILTER.toLowerCase();list=list.filter(e=>JSON.stringify([e.title,e.body,e.tags,e.files,e.project]).toLowerCase().includes(f))}
   list.sort((a,b)=>(b.created||'').localeCompare(a.created||''));
   m.innerHTML=`<div class="chips">${kinds.map(k=>`<span class="chip ${KIND===k?'on':''}" onclick="KIND='${KIND===k?'':k}';render()">${k}</span>`).join('')}
     <span style="width:12px"></span><span class="chip ${AUTHOR?'':'on'}" onclick="AUTHOR='';render()">all authors</span>${authors.slice(0,10).map(a=>`<span class="chip ${AUTHOR===a?'on':''}" onclick="AUTHOR='${AUTHOR===a?'':a}';render()">${esc(a)}</span>`).join('')}</div>
     <input type="search" placeholder="Search title, body, tags, files…" value="${esc(FILTER)}" oninput="FILTER=this.value;render()">
     <div>${list.length?(list.map(e=>`
     <div class="entry t-${e.type}">
       <h4 onclick="const p=this.parentElement.querySelector('pre');p.style.display=p.style.display==='block'?'none':'block'">${esc(e.title)} <span class="muted">(${e.type}${e.scope==='personal'?', personal':''})</span></h4>
       <div class="muted">by ${esc(e.author)} · ${ago(e.created)}${e.project?' · '+esc(e.project)+'@'+esc(e.branch||''):''} · conf ${e.confidence}${e.status!=='unverified'?' · '+esc(e.status):''}${e.superseded_by?' → '+esc(e.superseded_by):''}</div>
       ${(e.tags||[]).length?`<div>${e.tags.map(t=>`<span class="tag">#${esc(t)}</span>`).join('')}</div>`:''}
       ${(e.files||[]).length?`<div class="muted">files: ${e.files.map(f=>esc(f)).join(', ')}</div>`:''}
       <pre>${esc(e.body)}</pre>
     </div>`).join('')):'<div class="empty">No memories match</div>'}</div>`;
 }
 if(TAB==='profiles'){
   const ps=DATA.team.profiles||[];
   m.innerHTML='<div class="grid">'+(ps.length?ps.map(p=>`<div class="card"><h3>${esc(p.user)}</h3><div class="kv">
     <span class="muted">role</span><span>${esc(p.role||'-')}</span><span class="muted">tz</span><span>${esc(p.timezone||'-')}</span>
     <span class="muted">email</span><span>${esc(p.email||p.git_email||'-')}</span><span class="muted">git</span><span>${esc(p.git_name||'-')}</span></div>
     ${p.about?`<div style="margin-top:6px">${esc(p.about)}</div>`:''}</div>`).join(''):'<div class="empty">No profiles yet</div>')+'</div>';
 }
 if(TAB==='overview'){
   const c=DATA.team.counts||{};const total=Object.values(c).reduce((a,b)=>a+b,0);
   const byAuthor={},byProject={},byStatus={};
   [...(DATA.team.entries||[])].forEach(e=>{byAuthor[e.author]=(byAuthor[e.author]||0)+1;if(e.project)byProject[e.project]=(byProject[e.project]||0)+1;byStatus[e.status]=(byStatus[e.status]||0)+1});
   m.innerHTML=`<div class="grid">
   <div class="card"><h3>Totals</h3><div class="kv"><span class="muted">entries</span><span>${total}</span>
   ${Object.entries(c).map(([k,v])=>`<span class="muted">${k}s</span><span>${v}</span>`).join('')}</div></div>
   <div class="card"><h3>By author</h3><div class="kv">${Object.entries(byAuthor).map(([k,v])=>`<span class="muted">${esc(k)}</span><span>${v}</span>`).join('')||'<span class="muted">-</span><span></span>'}</div></div>
   <div class="card"><h3>By project</h3><div class="kv">${Object.entries(byProject).map(([k,v])=>`<span class="muted">${esc(k)}</span><span>${v}</span>`).join('')||'<span class="muted">-</span><span></span>'}</div></div>
   <div class="card"><h3>Lifecycle</h3><div class="kv">${Object.entries(byStatus).map(([k,v])=>`<span class="muted">${esc(k)}</span><span>${v}</span>`).join('')||'<span class="muted">-</span><span></span>'}</div></div>
   <div class="card"><h3>Storage</h3><div class="kv"><span class="muted">team repo</span><span>${esc(DATA.repo_name)}</span><span class="muted">branch</span><span>${esc(DATA.team_branch||'-')}</span><span class="muted">local clone</span><span style="word-break:break-all">${esc(DATA.team_local||'-')}</span><span class="muted">personal dir</span><span style="word-break:break-all">${esc(DATA.personal_local||'-')}</span></div></div>
   </div>`;
 }
}
load();setInterval(load,30000);
</script>
</body>
</html>"""


def build_dashboard(cfg: Config) -> dict:
    team = Store(cfg)
    personal = Store.personal(cfg)
    return {
        "team": team.snapshot(),
        "personal": _personal_only(personal),
        "repo_name": cfg.repo_name,
        "team_branch": team.branch,
        "team_local": str(team.path),
        "personal_local": str(personal.path),
        "meta": {
            "user": cfg.user,
            "project": team.project_git.repo.slug if team.available_project() else "",
            "branch": team.project_git.current_branch(),
        },
    }


def _personal_only(pstore: Store) -> dict:
    snap = pstore.snapshot()
    return {"entries": snap["entries"], "activity": snap["activity"], "statuses": snap["statuses"]}


class UiServer:
    def __init__(self, port: int = 8765):
        self.port = port
        self.data: dict | None = None
        self.error: str | None = None

    def reload(self) -> None:
        try:
            cfg = load_config()
            self.error = None
            self.data = build_dashboard(cfg)
        except (ConfigError, StoreError) as exc:
            self.error = str(exc)
            self.data = self.data or {
                "team": {"entries": [], "statuses": [], "activity": [], "profiles": [], "counts": {}},
                "personal": {"entries": [], "statuses": [], "activity": []},
                "repo_name": "(not configured)",
                "team_branch": "", "team_local": "", "personal_local": "", "meta": {},
            }

    def handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence request logging
                pass

            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/all":
                    outer.reload()
                    payload = outer.data or {}
                    if outer.error:
                        payload["error"] = outer.error
                    self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

        return H

    def serve(self, open_browser: bool = True) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", self.port), self.handler())
        url = f"http://127.0.0.1:{self.port}"
        print(f"Crew Memory dashboard: {url}  (Ctrl+C to stop)")
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


def cmd_ui(args) -> int:
    ui = UiServer(port=args.port)
    ui.reload()
    if ui.error:
        print(f"warning: {ui.error}")
        print("(dashboard will show setup hints until config is fixed)")
    ui.serve(open_browser=not args.no_browser)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="crewmemory-ui", description="Crew Memory web dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    sys_exit = cmd_ui(parser.parse_args())
    raise SystemExit(sys_exit)


if __name__ == "__main__":
    main()
