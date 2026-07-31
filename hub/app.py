"""The platform web app: login, admin (assign + monitor + manage workers),
and each worker's "my share" view. Run locally:  python -m hub.app
"""

import os
import functools

import io
import tempfile
import zipfile

from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, flash, abort, jsonify, send_file)

from .db import init_db
from . import engine, auth, s3source, drive
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
    <h2>QC summary</h2>
    <div style="display:flex;gap:36px;flex-wrap:wrap;margin-top:6px">
      <div><div class="muted" style="font-size:12px">Properly done</div>
        <div class="num" style="font-size:26px;font-weight:700;color:var(--good)">{{totals.done}}</div></div>
      <div><div class="muted" style="font-size:12px">Rectified by QC</div>
        <div class="num" style="font-size:26px;font-weight:700;color:var(--accent)">{{totals.rectified}}</div></div>
      <div><div class="muted" style="font-size:12px">Rejected</div>
        <div class="num" style="font-size:26px;font-weight:700;color:#c5423c">{{totals.rejected}}</div></div>
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
</div>
<style>.dlbtn,.upbtn{display:inline-block;padding:7px 14px;border-radius:8px;font-size:13px;border:0;cursor:pointer}
.dlbtn{background:var(--accent);color:#fff;text-decoration:none}
.upbtn{background:var(--good);color:#fff}</style>
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
  <div class="card">
    <h1>{{d.worker}} · {{d.brand}}</h1>
    <p class="muted" style="margin-top:0">{{d.photos|length}} photos. Mark each <b>Okay</b> or
       <b>Reject</b> (add a note + screenshot of the issue), or upload a quick <b>Fix</b> yourself.
       Click an image to open it full-size.</p>
    <div class="qgrid">
      {% for p in d.photos %}
      <div class="qtile">
        {% if p.thumb %}
          <a href="{{p.link}}" target="_blank"><img src="{{p.thumb}}" loading="lazy"
             onerror="this.parentNode.innerHTML='<div class=&quot;qph&quot;>preview unavailable — open in Drive</div>'"></a>
        {% else %}
          <div class="qph">{{ 'not uploaded yet' if p.status != 'uploaded' else 'no preview' }}</div>
        {% endif %}
        <div class="qcap">
          {% if p.qc=='ok' %}
            <span class="pill done">✓ okay</span>
          {% elif p.qc=='rectified' %}
            <span class="pill" style="background:var(--good-bg);color:var(--good)">rectified by QC</span>
            {% if p.link %}<a href="{{p.link}}" target="_blank" style="float:right;font-size:12px">view ↗</a>{% endif %}
          {% elif p.qc=='reject' %}
            <span class="pill rem">rejected → sent back</span>
            {% if p.qc_remark %}<div class="muted" style="font-size:12px;margin-top:4px">“{{p.qc_remark}}”</div>{% endif %}
          {% elif p.status=='uploaded' %}
            <div style="display:flex;gap:5px;flex-wrap:wrap">
              <form method="post" action="{{url_for('qc_photo_ok', pid=p.id)}}" style="margin:0"><button class="okb">Okay</button></form>
              <button class="rjb" onclick="tgl('r{{p.id}}')">Reject</button>
              <button class="fxb" onclick="tgl('f{{p.id}}')">Fix</button>
            </div>
            <form id="r{{p.id}}" class="hid" method="post" enctype="multipart/form-data" action="{{url_for('qc_photo_reject', pid=p.id)}}">
              <textarea name="remark" placeholder="what's the issue?" rows="2"></textarea>
              <label class="flab">screenshot (optional)<input type="file" name="shot" accept="image/*"></label>
              <button class="rjb" style="width:100%">Send back to worker</button>
            </form>
            <form id="f{{p.id}}" class="hid" method="post" enctype="multipart/form-data" action="{{url_for('qc_photo_rectify', pid=p.id)}}">
              <label class="flab">upload your fixed image<input type="file" name="file" accept="image/*" required></label>
              <button class="fxb" style="width:100%">Upload my fix</button>
            </form>
          {% else %}
            <span class="muted" style="font-size:12px">worker hasn't uploaded yet</span>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
</div>
<style>
  .qgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;margin-top:16px}
  .qtile{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card)}
  .qtile img{width:100%;height:160px;object-fit:cover;display:block;background:var(--line);cursor:zoom-in}
  .qph{height:160px;display:grid;place-items:center;text-align:center;color:var(--muted);font-size:12px;padding:0 10px;background:var(--card)}
  .qcap{padding:9px 10px}
  .okb{background:var(--good);color:#fff;border:0;border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer}
  .rjb{background:#c5423c;color:#fff;border:0;border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer}
  .fxb{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer}
  .hid{display:none;margin-top:8px}
  .qcap textarea{width:100%;font-size:12px;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
  .flab{display:block;font-size:11px;color:var(--muted);margin:6px 0}
  .flab input{display:block;font-size:11px;margin-top:2px}
</style>
<script>function tgl(id){var e=document.getElementById(id);e.style.display=e.style.display==='block'?'none':'block';}</script>
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
        msgs=_pop_flash())


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
    d = engine.assignment_detail(aid)
    if not d or d.get("qc_login") != session.get("login"):
        abort(403)          # a QC can only open batches assigned to them
    return render_template_string(QC_DETAIL, name=session.get("name"), d=d)


@app.route("/qc/photo/<int:pid>/ok", methods=["POST"])
@login_required
def qc_photo_ok(pid):
    if session.get("role") != ROLE_QC:
        abort(403)
    aid = engine.qc_mark_ok(pid, session.get("login"))
    if aid is None:
        abort(403)
    return redirect(url_for("qc_assignment_view", aid=aid))


@app.route("/qc/photo/<int:pid>/reject", methods=["POST"])
@login_required
def qc_photo_reject(pid):
    if session.get("role") != ROLE_QC:
        abort(403)
    if engine.photo_qc_login(pid) != session.get("login"):
        abort(403)
    remark = request.form.get("remark", "").strip()
    shot_link = ""
    f = request.files.get("shot")
    if f and f.filename:
        _, shot_link = drive.upload_file("QC_" + f.filename, f.mimetype,
                                         io.BytesIO(f.read()))
    aid = engine.qc_mark_reject(pid, session.get("login"), remark, shot_link)
    return redirect(url_for("qc_assignment_view", aid=aid or 0))


@app.route("/qc/photo/<int:pid>/rectify", methods=["POST"])
@login_required
def qc_photo_rectify(pid):
    if session.get("role") != ROLE_QC:
        abort(403)
    if engine.photo_qc_login(pid) != session.get("login"):
        abort(403)
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400)
    _, link = drive.upload_file(f.filename, f.mimetype, io.BytesIO(f.read()))
    aid = engine.qc_mark_rectify(pid, session.get("login"), link)
    return redirect(url_for("qc_assignment_view", aid=aid or 0))


@app.route("/me")
@login_required
def me():
    summary = engine.worker_summary(session.get("login")) or {"assignments": []}
    return render_template_string(WORKER, name=session.get("name"),
                                  login=session.get("login"), summary=summary)


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
