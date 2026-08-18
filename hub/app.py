"""The platform web app: login, admin (assign + monitor + manage workers),
and each worker's "my share" view. Run locally:  python -m hub.app
"""

import os
import functools

import io
import datetime as dt
import tempfile
import zipfile

from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, flash, abort, jsonify, send_file)

from .db import init_db
from . import engine, auth, s3source, drive, report
from .models import ROLE_ADMIN, ROLE_QC, ST_ASSIGNED, ST_DOWNLOADED

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

init_db()
auth.seed_admin()


# ---- brand suggestions (for the assign box) ------------------------------
def brand_suggestions():
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "brands.txt"),
                 os.path.join(here, "..", "brands.txt")):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return [ln.strip() for ln in f if ln.strip()]
    return []


# ---- auth guards ---------------------------------------------------------
def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not session.get("uid"):
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not session.get("uid"):
            return redirect(url_for("login"))
        if session.get("role") != ROLE_ADMIN:
            abort(403)
        return view(*a, **kw)
    return wrapped


# ---- shared styling ------------------------------------------------------
STYLE = """
<style>
  :root{--bg:#f5f6f9;--card:#fff;--ink:#19212e;--muted:#5b6676;--line:#e4e8ee;
    --accent:#2d57d4;--good:#1c9a61;--good-bg:#e6f5ee;--warn:#b9740f;--warn-bg:#fbf0de}
  @media(prefers-color-scheme:dark){:root{--bg:#0d131e;--card:#151d2b;--ink:#e9edf4;
    --muted:#98a3b4;--line:#243044;--accent:#7099ff;--good:#42c088;--good-bg:#132a20;
    --warn:#e0a54b;--warn-bg:#2c2213}}
  *{box-sizing:border-box}
  body{font-family:system-ui,'Segoe UI',Arial,sans-serif;background:var(--bg);color:var(--ink);
    margin:0;line-height:1.55}
  .top{background:var(--card);border-bottom:1px solid var(--line);padding:16px 40px;
    display:flex;justify-content:space-between;align-items:center}
  .top b{font-size:16px} .top .who{color:var(--muted);font-size:14px}
  .top a{color:var(--accent);text-decoration:none;font-size:14px;margin-left:14px}
  .wrap{max-width:1400px;margin:26px auto;padding:0 40px}
  @media(max-width:640px){.top{padding:14px 18px}.wrap{padding:0 18px}}
  h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:0 0 10px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:16px 0}
  label{font-size:13px;color:var(--muted);display:block;margin-bottom:3px}
  input,select{padding:9px 11px;border:1px solid var(--line);border-radius:8px;font-size:14px;
    background:var(--card);color:var(--ink);width:100%}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
  .row>div{flex:1;min-width:120px}
  button{padding:9px 18px;border:0;border-radius:8px;background:var(--accent);color:#fff;
    font-size:14px;cursor:pointer}
  table{border-collapse:collapse;width:100%;font-size:14px}
  th,td{text-align:left;padding:9px 11px;border-bottom:1px solid var(--line)}
  th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
  .track{height:8px;background:var(--line);border-radius:5px;overflow:hidden;width:110px}
  .fill{height:100%;background:var(--accent)}
  .seg{display:flex;height:9px;border-radius:5px;overflow:hidden;width:150px;background:var(--line)}
  .seg .d{background:var(--good)} .seg .w{background:var(--warn)}
  .legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-bottom:10px}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
  .pill{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;white-space:nowrap}
  .done{background:var(--good-bg);color:var(--good)} .rem{background:var(--warn-bg);color:var(--warn)}
  .flash{background:var(--good-bg);color:var(--good);padding:10px 14px;border-radius:8px;margin:12px 0;font-size:14px}
  .note{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;
    background:var(--warn-bg);border:1px solid var(--line);border-left:4px solid var(--accent);
    padding:12px 14px;border-radius:8px;margin:12px 0;font-size:14px}
  .note .xbtn{background:transparent;color:var(--muted);border:0;font-size:15px;cursor:pointer;padding:2px 8px;flex:0 0 auto}
  .muted{color:var(--muted)} .num{font-variant-numeric:tabular-nums}
  a{color:var(--accent)}
</style>
"""

LOGIN = STYLE + """
<div class="wrap" style="max-width:380px;margin-top:12vh">
  <div class="card">
    <h1>Photo Handout</h1>
    <p class="muted" style="margin-top:0;font-size:14px">Sign in to continue.</p>
    {% for m in msgs %}<div class="flash" style="background:#fdecea;color:#c5423c">{{m}}</div>{% endfor %}
    <form method="post" style="margin-top:8px">
      <div style="margin-bottom:12px"><label>Login</label><input name="login" autofocus></div>
      <div style="margin-bottom:16px"><label>Password</label><input name="password" type="password"></div>
      <button type="submit" style="width:100%">Sign in</button>
    </form>
  </div>
</div>
"""

ADMIN = STYLE + """
<div class="top"><b>Photo Handout · Admin</b>
  <span><span class="who">{{name}}</span><a href="{{url_for('logout')}}">Sign out</a></span></div>
<div class="wrap">
  {% for m in msgs %}<div class="flash">{{m}}</div>{% endfor %}
  {% for nt in notes %}<div class="note"><div>{{nt.message}}</div>
    <form method="post" action="{{url_for('dismiss_note', nid=nt.id)}}" style="margin:0"><button class="xbtn" title="Dismiss">✕</button></form></div>{% endfor %}

  <div class="card">
    <h2>Assign work</h2>
    <form method="post" action="{{url_for('assign')}}" style="max-width:940px">
      <div class="row">
        <div><label>Worker</label>
          <select name="worker" required>
            {% for w in workers %}<option value="{{w.login}}">{{w.name}} ({{w.login}})</option>{% endfor %}
          </select></div>
        <div><label>Brand</label>
          {% if brands %}
          <select name="brand" required>
            <option value="" disabled selected>Choose a brand…</option>
            {% for b in brands %}<option value="{{b}}">{{b}}</option>{% endfor %}
          </select>
          {% else %}
          <input name="brand" placeholder="catchall_ireland" required>
          {% endif %}</div>
        <div style="max-width:170px"><label>QC (optional)</label>
          <select name="qc">
            <option value="">— none —</option>
            {% for q in qcs %}<option value="{{q.login}}">{{q.name}}</option>{% endfor %}
          </select></div>
        <div style="max-width:100px"><label>Photos</label>
          <input name="count" type="number" min="1" placeholder="60" required></div>
        <div style="max-width:110px;flex:0"><button type="submit">Assign</button></div>
      </div>
      {% if not workers %}<p class="muted" style="font-size:13px;margin-bottom:0">Add a worker below first.</p>{% endif %}
    </form>
  </div>

  <div class="card">
    <h2>Reports</h2>
    <p class="muted" style="font-size:13px;margin-top:0">Download an Excel workbook of every worker's images allotted in a date range — a Summary sheet plus one sheet per worker, with allot/upload/re-upload times, QC verdicts &amp; assessor, and each worker's error rate.</p>
    <form method="get" action="{{url_for('admin_report')}}" style="max-width:940px">
      <div class="row">
        <div style="max-width:180px"><label>From</label>
          <input type="date" name="start" value="{{month_start}}" required></div>
        <div style="max-width:180px"><label>To</label>
          <input type="date" name="end" value="{{today}}" required></div>
        <div style="max-width:240px"><label>Worker (optional)</label>
          <select name="worker">
            <option value="">All workers</option>
            {% for w in workers %}<option value="{{w.login}}">{{w.name}} ({{w.login}})</option>{% endfor %}
          </select></div>
        <div style="max-width:170px;flex:0"><button type="submit">⬇ Download Excel</button></div>
      </div>
      <p class="muted" style="font-size:12px;margin-bottom:0">Range is by <b>allotment</b> date (e.g. 1st–31st). Times are UTC.</p>
    </form>
  </div>

  <div class="card">
    <h2>QC summary</h2>
    <div style="display:flex;gap:36px;flex-wrap:wrap;margin-top:6px">
      <div><div class="muted" style="font-size:12px">Approved</div>
        <div class="num" style="font-size:26px;font-weight:700;color:var(--good)">{{totals.done}}</div></div>
      <div><div class="muted" style="font-size:12px">Corrected by QC</div>
        <div class="num" style="font-size:26px;font-weight:700;color:var(--accent)">{{totals.rectified}}</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Timeline</h2>
    {% if not rows %}<p class="muted">No assignments yet.</p>{% else %}
    <div class="legend">
      <span>newest first ·</span>
      <span><i style="background:var(--good)"></i>done</span>
      <span><i style="background:var(--warn)"></i>retouching</span>
      <span><i style="background:var(--line)"></i>to download</span></div>
    <table>
      <tr><th>Assigned</th><th>Uploaded</th><th>Worker</th><th>QC</th><th>Brand</th><th>Step 1 · Retouch</th><th>Left</th><th>Step 2 · QC</th><th></th></tr>
      {% for r in rows %}
      <tr>
        <td class="muted num" style="white-space:nowrap">{{r.when}}</td>
        <td class="num" style="white-space:nowrap">{% if r.uploaded_when %}<span{% if r.remaining==0 %} style="color:var(--good)"{% endif %}>{{r.uploaded_when}}</span>{% else %}<span class="muted">—</span>{% endif %}</td>
        <td><b>{{r.worker}}</b></td>
        <td><form method="post" action="{{url_for('assign_qc_route')}}" style="margin:0">
              <input type="hidden" name="assignment_id" value="{{r.assignment_id}}">
              <select name="qc" onchange="this.form.submit()" style="padding:5px 8px;font-size:12px;max-width:140px">
                <option value="">{{ '— remove QC —' if r.qc else '— assign QC —' }}</option>
                {% for q in qcs %}<option value="{{q.login}}" {{'selected' if q.name==r.qc else ''}}>{{q.name}}</option>{% endfor %}
              </select></form></td>
        <td>{{r.brand}}</td>
        <td><div style="display:flex;gap:8px;align-items:center">
          <div class="seg">
            <div class="d" style="width:{{ (100*r.uploaded/r.assigned)|int if r.assigned else 0 }}%"></div>
            <div class="w" style="width:{{ (100*r.downloaded/r.assigned)|int if r.assigned else 0 }}%"></div>
          </div>
          <span class="num muted">{{r.uploaded}}/{{r.assigned}}</span></div></td>
        <td>{% if r.remaining==0 and r.assigned>0 %}<span class="pill done">done</span>
            {% else %}<span class="pill rem">{{r.remaining}}</span>{% endif %}</td>
        <td>{% if r.stage=='worker' %}<span class="muted" style="font-size:12px">waiting</span>
            {% elif r.stage=='complete' %}<span class="pill done">✓ QC done</span>
            {% else %}<span class="pill" style="background:var(--warn-bg);color:var(--warn)">QC {{r.qc_done}}/{{r.assigned}}</span>{% endif %}
            {% if r.qc_rectified %}<div style="font-size:11px;color:var(--accent)">{{r.qc_rectified}} corrected</div>{% endif %}
            {% if r.qc_rejected %}<div style="font-size:11px;color:#c5423c">{{r.qc_rejected}} rejected</div>{% endif %}
            {% if r.qc_pending %}<div style="font-size:11px" class="muted">{{r.qc_pending}} to review</div>{% endif %}</td>
        <td><a href="{{url_for('assignment_view', aid=r.assignment_id)}}">details</a></td>
      </tr>
      {% endfor %}
    </table>{% endif %}
  </div>

  <div class="card">
    <h2>Team</h2>
    <div class="muted" style="font-size:13px;margin-bottom:10px">
      {{n_workers}} worker{{ 's' if n_workers != 1 else '' }} · {{n_qc}} QC</div>
    <table>
      <tr><th>Name</th><th>Login</th><th>Role</th></tr>
      {% for w in workers_all %}<tr><td>{{w.name}}</td><td class="muted">{{w.login}}</td>
        <td>{% if w.role=='qc' %}<span class="pill" style="background:var(--warn-bg);color:var(--warn)">QC</span>
            {% elif w.role=='admin' %}<span class="muted">admin</span>
            {% else %}<span class="pill" style="background:var(--good-bg);color:var(--good)">worker</span>{% endif %}</td></tr>{% endfor %}
    </table>
    <form method="post" action="{{url_for('add_worker')}}" style="margin-top:16px;max-width:900px">
      <div class="row">
        <div><label>Name</label><input name="name" placeholder="Yash" required></div>
        <div><label>Login</label><input name="login" placeholder="yash" required></div>
        <div><label>Password</label><input name="password" required></div>
        <div style="max-width:130px"><label>Role</label>
          <select name="role"><option value="worker">Worker</option><option value="qc">QC</option></select></div>
        <div style="max-width:100px;flex:0"><button type="submit">Add</button></div>
      </div>
    </form>
  </div>
</div>
"""

WORKER = STYLE + """
<div class="top"><b>Photo Handout · My work</b>
  <span><span class="who">{{name}}</span><a href="{{url_for('logout')}}">Sign out</a></span></div>
<div class="wrap">
  {% for nt in notes %}<div class="note"><div>{{nt.message}}</div>
    <form method="post" action="{{url_for('dismiss_note', nid=nt.id)}}" style="margin:0"><button class="xbtn" title="Dismiss">✕</button></form></div>{% endfor %}
  <div class="card">
    <h1>Hello, {{name}}</h1>
    {% if not summary.assignments %}<p class="muted">No work assigned to you yet.</p>{% else %}
    <div class="legend">
      <span><i style="background:var(--good)"></i>done</span>
      <span><i style="background:var(--warn)"></i>to retouch</span>
      <span><i style="background:var(--line)"></i>to download</span></div>
    <table style="margin-top:6px">
      <tr><th>Assigned</th><th>Uploaded</th><th>Brand</th><th>Progress</th><th>What's left</th></tr>
      {% for a in summary.assignments %}
      <tr>
        <td class="muted num" style="white-space:nowrap">{{a.when}}</td>
        <td class="num" style="white-space:nowrap">{% if a.uploaded_when %}<span{% if a.complete %} style="color:var(--good)"{% endif %}>{{a.uploaded_when}}</span>{% else %}<span class="muted">—</span>{% endif %}</td>
        <td><b>{{a.brand}}</b></td>
        <td><div style="display:flex;gap:8px;align-items:center">
          <div class="seg">
            <div class="d" style="width:{{ (100*a.uploaded/a.assigned)|int if a.assigned else 0 }}%"></div>
            <div class="w" style="width:{{ (100*a.downloaded/a.assigned)|int if a.assigned else 0 }}%"></div>
          </div>
          <span class="num muted">{{a.uploaded}}/{{a.assigned}}</span></div></td>
        <td>{% if a.complete %}<span class="pill done">all done</span>
            {% else %}
              <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
                <a class="dlbtn" href="/me/download-zip/{{a.assignment_id}}">⬇ Download {{a.remaining}}</a>
                <button class="upbtn" onclick="document.getElementById('f{{a.assignment_id}}').click()">⬆ Upload finished</button>
                <input type="file" id="f{{a.assignment_id}}" multiple style="display:none"
                       onchange="up({{a.assignment_id}}, this)">
                {% if a.uploaded %}<span class="pill done">{{a.uploaded}} done</span>{% endif %}
                <span class="pill rem">{{a.remaining}} left</span>
              </div>
              <div id="msg{{a.assignment_id}}" class="muted" style="font-size:12px;margin-top:6px"></div>
            {% endif %}</td>
      </tr>
      {% endfor %}
    </table>
    <p class="muted" style="font-size:12px;margin-top:14px">Download the zip, retouch, then <b>Upload finished</b> and pick your finished files. Each finished file is matched to its photo by name — anything you still owe stays under “left”, and your admin sees the same.</p>
    {% endif %}
  </div>

  {% set nrej = summary.assignments | map(attribute='rejected') | sum %}
  {% if nrej %}
  <div class="card">
    <h2 style="color:#c5423c">Sent back by QC — please redo ({{nrej}})</h2>
    {% for a in summary.assignments %}{% for r in a.rejected_list %}
    <div style="display:flex;gap:14px;padding:12px 0;border-top:1px solid var(--line);align-items:flex-start">
      {% if r.shot_thumb %}<a href="{{r.shot}}" target="_blank"><img src="{{r.shot_thumb}}" style="width:96px;height:66px;object-fit:cover;border-radius:6px;border:1px solid var(--line)" onerror="this.style.display='none'"></a>{% endif %}
      <div>
        <div><b>{{a.brand}}</b> · <span class="num muted" style="font-size:12px">{{r.name}}</span></div>
        <div style="margin-top:2px"><b>Issue:</b> {{ r.remark or '(no note left)' }}</div>
        <div class="muted" style="font-size:12px;margin-top:2px">Redo this photo, then upload the finished file again — it goes back to QC.</div>
      </div>
    </div>
    {% endfor %}{% endfor %}
  </div>
  {% endif %}

  {% if corrections %}
  <div class="card">
    <h2>What QC corrected — learn from these ({{corrections|length}})</h2>
    <p class="muted" style="font-size:13px;margin-top:0">These are images a QC had to fix. Study them so the same issues don't come back.</p>
    <div class="cgrid">
      {% for c in corrections %}
      <div class="ctile">
        {% if c.thumb %}<a href="{{c.link}}" target="_blank"><img src="{{c.thumb}}" loading="lazy" onerror="this.parentNode.innerHTML='<div class=&quot;cph&quot;>open in Drive</div>'"></a>
        {% else %}<div class="cph">{% if c.link %}<a href="{{c.link}}" target="_blank">open in Drive</a>{% else %}no preview{% endif %}</div>{% endif %}
        <div style="padding:8px 10px"><b style="font-size:12px">{{c.brand}}</b>
          <div class="num muted" style="font-size:11px">{{c.name}}</div></div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
</div>
<style>.dlbtn,.upbtn{display:inline-block;padding:7px 14px;border-radius:8px;font-size:13px;border:0;cursor:pointer}
.dlbtn{background:var(--accent);color:#fff;text-decoration:none}
.upbtn{background:var(--good);color:#fff}
.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-top:12px}
.ctile{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card)}
.ctile img{width:100%;height:130px;object-fit:cover;display:block;background:var(--line);cursor:zoom-in}
.cph{height:130px;display:grid;place-items:center;color:var(--muted);font-size:12px;background:var(--card)}</style>
<script>
async function up(aid, input){
  const files = Array.from(input.files); input.value = '';
  if(!files.length) return;
  const box = document.getElementById('msg' + aid);
  let ok = 0; const skipped = [];
  for(let i = 0; i < files.length; i++){
    box.textContent = 'Uploading ' + (i + 1) + ' of ' + files.length + '…';
    const fd = new FormData(); fd.append('file', files[i]);
    try{
      const r = await fetch('/me/upload-one/' + aid, {method:'POST', body: fd});
      const d = await r.json();
      if(d.matched) ok++; else skipped.push(files[i].name);
    }catch(e){ skipped.push(files[i].name + ' (failed)'); }
  }
  let msg = ok + ' uploaded & verified.';
  if(skipped.length) msg += ' ' + skipped.length + " didn't match an assigned photo (ignored).";
  box.textContent = msg + ' Refreshing…';
  setTimeout(() => location.reload(), 1500);
}
</script>
"""

ASSIGN_DETAIL = STYLE + """
<div class="top"><b>Photo Handout · Assignment</b>
  <span><span class="who">{{name}}</span><a href="{{url_for('logout')}}">Sign out</a></span></div>
<div class="wrap">
  <a href="{{url_for('admin')}}">← back to dashboard</a>
  <div class="card">
    <h1>{{d.worker}} · {{d.brand}}</h1>
    <p class="muted" style="margin-top:0">{{d.photos|length}} photos · assigned {{d.when}}</p>
    <table>
      <tr><th>Photo</th><th>Status</th><th>Finished file</th></tr>
      {% for p in d.photos %}
      <tr>
        <td class="num" style="font-size:12px">{{p.name}}</td>
        <td>{% if p.status=='uploaded' %}<span class="pill done">done</span>
            {% elif p.status=='downloaded' %}<span class="pill rem">retouching</span>
            {% else %}<span class="muted">to download</span>{% endif %}</td>
        <td>{% if p.link %}<a href="{{p.link}}" target="_blank">open in Drive</a>{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>
"""

QC = STYLE + """
<div class="top"><b>Photo Handout · QC</b>
  <span><span class="who">{{name}}</span><a href="{{url_for('logout')}}">Sign out</a></span></div>
<div class="wrap">
  <div class="card">
    <h1>Hello, {{name}}</h1>
    <p class="muted" style="margin-top:0">Workers assigned to you for quality check, and their live status.</p>
    {% if not rows %}<p class="muted">Nothing assigned to you for review yet.</p>{% else %}
    <div class="legend"><span>newest first ·</span>
      <span><i style="background:var(--good)"></i>done</span>
      <span><i style="background:var(--warn)"></i>retouching</span>
      <span><i style="background:var(--line)"></i>to download</span></div>
    <table>
      <tr><th>Assigned</th><th>Uploaded</th><th>Worker</th><th>Brand</th><th>Progress</th><th>Status</th><th></th></tr>
      {% for r in rows %}
      <tr>
        <td class="muted num" style="white-space:nowrap">{{r.when}}</td>
        <td class="num" style="white-space:nowrap">{% if r.uploaded_when %}<span{% if r.complete %} style="color:var(--good)"{% endif %}>{{r.uploaded_when}}</span>{% else %}<span class="muted">—</span>{% endif %}</td>
        <td><b>{{r.worker}}</b></td>
        <td>{{r.brand}}</td>
        <td><div style="display:flex;gap:8px;align-items:center">
          <div class="seg">
            <div class="d" style="width:{{ (100*r.uploaded/r.assigned)|int if r.assigned else 0 }}%"></div>
            <div class="w" style="width:{{ (100*r.downloaded/r.assigned)|int if r.assigned else 0 }}%"></div>
          </div><span class="num muted">{{r.uploaded}}/{{r.assigned}}</span></div></td>
        <td>{% if r.complete %}<span class="pill done">ready to check</span>
            {% else %}<span class="pill rem">{{r.remaining}} pending</span>{% endif %}</td>
        <td><a class="dlbtn" style="background:var(--accent);color:#fff;padding:6px 12px;border-radius:8px;text-decoration:none;font-size:13px" href="{{url_for('qc_assignment_view', aid=r.assignment_id)}}">Details check</a></td>
      </tr>
      {% endfor %}
    </table>
    <p class="muted" style="font-size:12px;margin-top:14px">“Ready to check” means the worker has uploaded everything. Click <b>Details check</b> to view the images.</p>
    {% endif %}
  </div>
</div>
"""

QC_DETAIL = STYLE + """
<div class="top"><b>Photo Handout · QC review</b>
  <span><span class="who">{{name}}</span><a href="{{url_for('logout')}}">Sign out</a></span></div>
<div class="wrap">
  <a href="{{url_for('qc')}}">← back to my list</a>
  {% for m in msgs %}<div class="flash">{{m}}</div>{% endfor %}

  <div class="card">
    <h1>{{d.worker}} · {{d.brand}}</h1>
    <p class="muted" style="margin-top:0">{{d.uploaded}} of {{d.total}} photos uploaded by the worker.
      {% if not d.all_uploaded %}<b style="color:var(--warn)">The worker hasn't finished uploading yet.</b>{% endif %}</p>

    {% if d.review %}
    <div class="flash" style="background:var(--good-bg);color:var(--good)">
      Reviewed {{d.review.when}} — <b>{{d.review.total}}</b> checked, <b>{{d.review.corrected}}</b> corrected
      (<b>{{d.review.rate}}%</b> error rate).{% if d.review.note %} Note to worker: “{{d.review.note}}”.{% endif %}
      Submitting again will update it.</div>
    {% endif %}

    <div style="margin:14px 0">
      <a class="dlbtn" href="{{url_for('qc_download_zip', aid=d.assignment_id)}}">⬇ Download all ({{d.uploaded}})</a>
    </div>
    <p class="muted" style="font-size:12px;margin:0">Downloads the worker's finished files as one zip. Check them, fix the bad ones, then upload below.</p>
  </div>

  <div class="card">
    <h2>Submit your review</h2>
    <form method="post" action="{{url_for('qc_submit_batch', aid=d.assignment_id)}}" enctype="multipart/form-data" id="qcform">
      <div class="row" style="align-items:flex-start">
        <div>
          <label>Box 1 · Total — all {{d.uploaded}} reviewed files</label>
          <input type="file" name="total" multiple>
          <div class="muted" style="font-size:12px;margin-top:4px">Upload the whole reviewed batch. These become the final stored files.</div>
        </div>
        <div>
          <label>Box 2 · Corrected — only the ones you fixed</label>
          <input type="file" name="corrected" multiple>
          <div class="muted" style="font-size:12px;margin-top:4px">Just the images you had to correct — these set the error rate.</div>
        </div>
      </div>
      <div style="margin-top:12px;max-width:640px">
        <label>Note to the worker (optional)</label>
        <input name="note" placeholder="e.g. watch the reflections on the wheels">
      </div>
      <div style="margin-top:14px">
        <button type="submit" id="qcbtn">Submit review &amp; notify</button>
        <span id="qcmsg" class="muted" style="font-size:13px;margin-left:10px"></span>
      </div>
    </form>
  </div>

  {% if d.photos %}
  <div class="card">
    <h2>Worker's uploads ({{d.photos|length}})</h2>
    <div class="qgrid">
      {% for p in d.photos %}
      <div class="qtile">
        {% if p.thumb %}
          <a href="{{p.link}}" target="_blank"><img src="{{p.thumb}}" loading="lazy"
             onerror="this.parentNode.innerHTML='<div class=&quot;qph&quot;>open in Drive</div>'"></a>
        {% else %}<div class="qph">no preview</div>{% endif %}
        <div class="qcap">
          <span class="num" style="font-size:11px">{{p.name}}</span>
          {% if p.qc=='rectified' %}<span class="pill" style="background:var(--good-bg);color:var(--good);float:right">corrected</span>
          {% elif p.qc=='ok' %}<span class="pill done" style="float:right">okay</span>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
</div>
<style>
  .dlbtn{display:inline-block;padding:8px 16px;border-radius:8px;font-size:14px;background:var(--accent);color:#fff;text-decoration:none}
  .qgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-top:12px}
  .qtile{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card)}
  .qtile img{width:100%;height:140px;object-fit:cover;display:block;background:var(--line);cursor:zoom-in}
  .qph{height:140px;display:grid;place-items:center;color:var(--muted);font-size:12px;background:var(--card)}
  .qcap{padding:8px 10px;overflow:hidden}
</style>
<script>
  document.getElementById('qcform').addEventListener('submit', function(){
    document.getElementById('qcbtn').disabled = true;
    document.getElementById('qcmsg').textContent = 'Uploading & saving… this can take a moment for large batches.';
  });
</script>
"""


# ---- routes --------------------------------------------------------------
@app.route("/")
def home():
    if not session.get("uid"):
        return redirect(url_for("login"))
    role = session.get("role")
    if role == ROLE_ADMIN:
        return redirect(url_for("admin"))
    if role == ROLE_QC:
        return redirect(url_for("qc"))
    return redirect(url_for("me"))


@app.route("/login", methods=["GET", "POST"])
def login():
    msgs = []
    if request.method == "POST":
        u = auth.authenticate(request.form.get("login", ""),
                              request.form.get("password", ""))
        if u:
            session["uid"] = u["id"]
            session["role"] = u["role"]
            session["name"] = u["name"]
            session["login"] = u["login"]
            return redirect(url_for("home"))
        msgs.append("Wrong login or password.")
    return render_template_string(LOGIN, msgs=msgs)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def admin():
    everyone = auth.list_workers()
    today = dt.date.today()
    return render_template_string(
        ADMIN, name=session.get("name"),
        rows=engine.admin_overview(),
        totals=engine.totals(),
        workers=auth.list_workers(role="worker"),
        qcs=auth.list_workers(role="qc"),
        workers_all=everyone,
        n_workers=sum(1 for w in everyone if w["role"] == "worker"),
        n_qc=sum(1 for w in everyone if w["role"] == "qc"),
        brands=brand_suggestions(),
        today=today.isoformat(),
        month_start=today.replace(day=1).isoformat(),
        notes=engine.unread_notifications(session.get("login")),
        msgs=_pop_flash())


@app.route("/admin/report")
@admin_required
def admin_report():
    """Build and stream an .xlsx report for a date range (by allotment date),
    optionally for one worker."""
    start_s = request.args.get("start", "").strip()
    end_s = request.args.get("end", "").strip()
    worker = request.args.get("worker", "").strip()
    try:
        start = dt.datetime.strptime(start_s, "%Y-%m-%d")
        end_day = dt.datetime.strptime(end_s, "%Y-%m-%d")
    except ValueError:
        _flash("Pick a valid From and To date for the report.")
        return redirect(url_for("admin"))
    end = end_day + dt.timedelta(days=1)          # make the To date inclusive
    if end <= start:
        _flash("Report: the 'To' date must be on or after 'From'.")
        return redirect(url_for("admin"))

    data = engine.report_data(start, end, worker or None)
    if not data["master"]:
        who = f" for {worker}" if worker else ""
        _flash(f"No photos were allotted{who} between {start_s} and {end_s}.")
        return redirect(url_for("admin"))

    buf = report.build_xlsx(data)
    tag = ("_" + worker) if worker else ""
    fname = f"worker-report_{start_s}_to_{end_s}{tag}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument"
                 ".spreadsheetml.sheet")


@app.route("/admin/assign", methods=["POST"])
@admin_required
def assign():
    worker = request.form.get("worker", "").strip()
    brand = request.form.get("brand", "").strip()
    count = (request.form.get("count") or "").strip()
    qc = request.form.get("qc", "").strip()
    if worker and brand and count.isdigit() and int(count) > 0:
        res = engine.create_assignment(worker, brand, int(count),
                                       qc_login=qc or None)
        msg = f"Assigned {res['reserved']} photo(s) of {brand} to {res['worker']}."
        if qc:
            msg += f" QC: {qc}."
        if res["short"]:
            msg += f" ({res['short']} short — not enough available.)"
        _flash(msg)
    else:
        _flash("Fill in worker, brand and a positive number.")
    return redirect(url_for("admin"))


@app.route("/admin/workers", methods=["POST"])
@admin_required
def add_worker():
    name = request.form.get("name", "").strip()
    login_ = request.form.get("login", "").strip()
    pw = request.form.get("password", "").strip()
    role = request.form.get("role", "worker").strip().lower()
    if role not in ("worker", "qc"):
        role = "worker"
    if name and login_ and pw:
        r = auth.create_user(name, login_, pw, role=role)
        label = "QC" if r["role"] == "qc" else "worker"
        _flash(f"{'Updated' if r['updated'] else 'Added'} {label} {r['name']} ({r['login']}).")
    else:
        _flash("Name, login and password are all required.")
    return redirect(url_for("admin"))


@app.route("/admin/assignment/<int:aid>")
@admin_required
def assignment_view(aid):
    d = engine.assignment_detail(aid)
    if not d:
        return redirect(url_for("admin"))
    return render_template_string(ASSIGN_DETAIL, name=session.get("name"), d=d)


@app.route("/admin/assign-qc", methods=["POST"])
@admin_required
def assign_qc_route():
    aid = request.form.get("assignment_id", "")
    qc = request.form.get("qc", "").strip()
    if aid.isdigit():
        engine.assign_qc(int(aid), qc or None)
    return redirect(url_for("admin"))


@app.route("/qc")
@login_required
def qc():
    if session.get("role") != ROLE_QC:
        return redirect(url_for("home"))
    return render_template_string(QC, name=session.get("name"),
                                  rows=engine.qc_assignments(session.get("login")))


@app.route("/qc/assignment/<int:aid>")
@login_required
def qc_assignment_view(aid):
    if session.get("role") != ROLE_QC:
        return redirect(url_for("home"))
    d = engine.qc_batch_view(aid)
    if not d or d.get("qc_login") != session.get("login"):
        abort(403)          # a QC can only open batches assigned to them
    return render_template_string(QC_DETAIL, name=session.get("name"), d=d,
                                  msgs=_pop_flash())


@app.route("/qc/download-zip/<int:aid>")
@login_required
def qc_download_zip(aid):
    """Zip the worker's finished files (streamed from Drive) so the QC can pull
    the whole batch at once and check it offline."""
    if session.get("role") != ROLE_QC:
        abort(403)
    info = engine.qc_batch_download(aid, session.get("login"))
    if info is None:
        abort(403)
    if not info["files"]:
        _flash("Nothing to download yet — the worker hasn't uploaded any "
               "finished files.")
        return redirect(url_for("qc_assignment_view", aid=aid))

    # resolve real Drive names first (keeps extensions), de-duplicating clashes
    entries, seen = [], {}
    for it in info["files"]:
        nm = drive.file_name(it["file_id"], default=it["name"])
        if nm in seen:
            seen[nm] += 1
            root, dot, ext = nm.rpartition(".")
            nm = f"{root}_{seen[nm]}.{ext}" if dot else f"{nm}_{seen[nm]}"
        else:
            seen[nm] = 1
        entries.append((it["file_id"], nm))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
            for fid, nm in entries:
                drive.stream_into_zip(zf, fid, nm)
    except Exception:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise

    resp = send_file(tmp.name, as_attachment=True, mimetype="application/zip",
                     download_name=f"{info['brand']}_assignment{aid}_QC.zip")

    @resp.call_on_close
    def _cleanup():
        try:
            os.remove(tmp.name)
        except OSError:
            pass

    return resp


@app.route("/qc/submit-batch/<int:aid>", methods=["POST"])
@login_required
def qc_submit_batch(aid):
    """Whole-batch QC review: Box 1 = the full reviewed set (becomes the final
    stored files, replacing the worker's), Box 2 = the ones the QC corrected
    (drives the error rate). Notifies the worker + admin."""
    if session.get("role") != ROLE_QC:
        abort(403)
    d = engine.qc_batch_view(aid)
    if not d or d.get("qc_login") != session.get("login"):
        abort(403)

    total_files = [f for f in request.files.getlist("total") if f and f.filename]
    corrected_files = [f for f in request.files.getlist("corrected")
                       if f and f.filename]
    corrected_names = {f.filename for f in corrected_files}

    total_links = {}
    for f in total_files:                      # Box 1 → the final stored set
        _, link = drive.upload_file(f.filename, f.mimetype, io.BytesIO(f.read()))
        total_links[f.filename] = link
    for f in corrected_files:                  # ensure Box 2 files are stored too
        if f.filename not in total_links:
            _, link = drive.upload_file(f.filename, f.mimetype,
                                        io.BytesIO(f.read()))
            total_links[f.filename] = link

    if not total_links:
        _flash("Add your files to Box 1 (and the corrected ones to Box 2) "
               "before submitting.")
        return redirect(url_for("qc_assignment_view", aid=aid))

    result = engine.submit_qc_batch(aid, session.get("login"), total_links,
                                    corrected_names,
                                    request.form.get("note", "").strip())
    if result is None:
        abort(403)
    # the worker's replaced files are now stale — drop them so each photo is
    # stored only once (best effort; a leftover copy is harmless)
    for link in result["old_links"]:
        fid = engine.file_id_from_link(link)
        if fid:
            drive.delete_file(fid)

    _flash(f"Review saved: {result['total']} checked, {result['corrected']} "
           f"corrected ({result['rate']}% error rate). Worker and admin "
           f"notified.")
    return redirect(url_for("qc_assignment_view", aid=aid))


@app.route("/notifications/<int:nid>/dismiss", methods=["POST"])
@login_required
def dismiss_note(nid):
    engine.mark_notification_read(nid, session.get("login"))
    return redirect(request.referrer or url_for("home"))


@app.route("/me")
@login_required
def me():
    login = session.get("login")
    summary = engine.worker_summary(login) or {"assignments": []}
    return render_template_string(
        WORKER, name=session.get("name"), login=login, summary=summary,
        notes=engine.unread_notifications(login),
        corrections=engine.worker_corrections(login))


@app.route("/me/download-zip/<int:aid>")
@login_required
def me_download_zip(aid):
    """Stream every not-yet-finished photo in this assignment as ONE zip
    (image + PSD, in folders). Files come from S3 through the server only for
    the moment it takes to zip them. Marks them downloaded on success."""
    if engine.assignment_owner_login(aid) != session.get("login"):
        abort(403)
    detail = engine.assignment_detail(aid)
    pids = (engine.assignment_photo_ids(aid, ST_ASSIGNED)
            + engine.assignment_photo_ids(aid, ST_DOWNLOADED))
    if not pids:
        abort(404)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
            for pid in pids:
                for key in s3source.files_for_photo(pid):
                    s3source.fetch_into_zip(zf, key, s3source.local_pid(key))
    except Exception:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise
    engine.mark_downloaded(aid, pids)

    resp = send_file(tmp.name, as_attachment=True, mimetype="application/zip",
                     download_name=f"{detail['brand']}_assignment{aid}.zip")

    @resp.call_on_close
    def _cleanup():
        try:
            os.remove(tmp.name)
        except OSError:
            pass

    return resp


@app.route("/me/upload-one/<int:aid>", methods=["POST"])
@login_required
def me_upload_one(aid):
    """Receive ONE finished file, match it by name to an assigned photo, push it
    to Drive, and mark that photo done. Uploading one-at-a-time keeps each
    request small and lets the page show live progress."""
    if engine.assignment_owner_login(aid) != session.get("login"):
        abort(403)
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    matched, _ = engine.match_uploads(aid, [f.filename])
    pid = matched.get(f.filename)
    if not pid:
        return jsonify({"matched": False, "filename": f.filename})
    fid, link = drive.upload_file(f.filename, f.mimetype, io.BytesIO(f.read()))
    engine.mark_uploaded(aid, [pid], {pid: link})
    return jsonify({"matched": True, "filename": f.filename,
                    "progress": engine.assignment_progress(aid)})


# ---- API for the worker companion (HTTP Basic auth) ----------------------
def _api_worker():
    a = request.authorization
    return auth.authenticate(a.username, a.password) if a else None


@app.route("/api/my-work")
def api_my_work():
    u = _api_worker()
    if not u:
        return jsonify({"error": "sign-in required"}), 401
    return jsonify({"worker": u["name"], "login": u["login"],
                    "assignments": engine.worker_assignments(u["login"])})


@app.route("/api/assignment/<int:aid>/download-urls")
def api_download_urls(aid):
    u = _api_worker()
    if not u:
        return jsonify({"error": "sign-in required"}), 401
    if engine.assignment_owner_login(aid) != u["login"]:
        return jsonify({"error": "not your assignment"}), 403
    photos = []
    for pid in engine.assignment_photo_ids(aid, status=ST_ASSIGNED):
        files = [{"key": key,
                  "path": s3source.local_pid(key),
                  "url": s3source.presigned_get(key)}
                 for key in s3source.files_for_photo(pid)]
        photos.append({"photo_id": pid, "files": files})
    return jsonify({"assignment_id": aid, "photos": photos})


@app.route("/api/assignment/<int:aid>/downloaded", methods=["POST"])
def api_mark_downloaded(aid):
    u = _api_worker()
    if not u:
        return jsonify({"error": "sign-in required"}), 401
    if engine.assignment_owner_login(aid) != u["login"]:
        return jsonify({"error": "not your assignment"}), 403
    data = request.get_json(force=True, silent=True) or {}
    n = engine.mark_downloaded(aid, data.get("photo_ids", []))
    return jsonify({"marked": n})


@app.route("/api/assignment/<int:aid>/upload-sessions", methods=["POST"])
def api_upload_sessions(aid):
    u = _api_worker()
    if not u:
        return jsonify({"error": "sign-in required"}), 401
    if engine.assignment_owner_login(aid) != u["login"]:
        return jsonify({"error": "not your assignment"}), 403
    valid = set(engine.assignment_photo_ids(aid))  # only this assignment's photos
    data = request.get_json(force=True, silent=True) or {}
    sessions = []
    for it in data.get("files", []):
        pid, fname = it.get("photo_id"), it.get("filename")
        if pid not in valid or not fname:
            continue
        url = drive.init_upload_session(
            fname, it.get("mimetype", "application/octet-stream"))
        sessions.append({"photo_id": pid, "filename": fname, "session_url": url})
    return jsonify({"sessions": sessions})


@app.route("/api/assignment/<int:aid>/uploaded", methods=["POST"])
def api_uploaded(aid):
    u = _api_worker()
    if not u:
        return jsonify({"error": "sign-in required"}), 401
    if engine.assignment_owner_login(aid) != u["login"]:
        return jsonify({"error": "not your assignment"}), 403
    valid = set(engine.assignment_photo_ids(aid))
    data = request.get_json(force=True, silent=True) or {}
    links = {}
    for it in data.get("items", []):
        pid, fid = it.get("photo_id"), it.get("file_id")
        if pid in valid and fid:
            links[pid] = drive.finalize(fid)          # share + get link
    n = engine.mark_uploaded(aid, list(links.keys()), links)
    return jsonify({"marked": n, "progress": engine.assignment_progress(aid)})


# tiny flash helper (session-based, avoids Flask's category noise)
def _flash(msg):
    session.setdefault("_msgs", [])
    session["_msgs"].append(msg)
    session.modified = True


def _pop_flash():
    m = session.pop("_msgs", [])
    return m


if __name__ == "__main__":
    # port 5001 so it never clashes with the desktop tool on 5000
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
