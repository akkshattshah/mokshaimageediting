"""The platform web app: login, admin (assign + monitor + manage workers),
and each worker's "my share" view. Run locally:  python -m hub.app
"""

import os
import functools

import tempfile
import zipfile

from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, flash, abort, jsonify, send_file)

from .db import init_db
from . import engine, auth, s3source, drive
from .models import ROLE_ADMIN, ST_ASSIGNED, ST_DOWNLOADED

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
  .top{background:var(--card);border-bottom:1px solid var(--line);padding:14px 22px;
    display:flex;justify-content:space-between;align-items:center}
  .top b{font-size:16px} .top .who{color:var(--muted);font-size:14px}
  .top a{color:var(--accent);text-decoration:none;font-size:14px;margin-left:14px}
  .wrap{max-width:820px;margin:26px auto;padding:0 18px}
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
    <form method="post" action="{{url_for('assign')}}">
      <div class="row">
        <div><label>Worker</label>
          <select name="worker" required>
            {% for w in workers %}<option value="{{w.login}}">{{w.name}} ({{w.login}})</option>{% endfor %}
          </select></div>
        <div><label>Brand</label>
          <input name="brand" list="brands" placeholder="catchall_ireland" required>
          <datalist id="brands">{% for b in brands %}<option value="{{b}}">{% endfor %}</datalist></div>
        <div style="max-width:110px"><label>Photos</label>
          <input name="count" type="number" min="1" placeholder="60" required></div>
        <div style="max-width:120px;flex:0"><button type="submit">Assign</button></div>
      </div>
      {% if not workers %}<p class="muted" style="font-size:13px;margin-bottom:0">Add a worker below first.</p>{% endif %}
    </form>
  </div>

  <div class="card">
    <h2>Progress</h2>
    {% if not rows %}<p class="muted">No assignments yet.</p>{% else %}
    <div class="legend">
      <span><i style="background:var(--good)"></i>done</span>
      <span><i style="background:var(--warn)"></i>retouching</span>
      <span><i style="background:var(--line)"></i>to download</span></div>
    <table>
      <tr><th>Worker</th><th>Brand</th><th>Progress</th><th>Status</th><th></th></tr>
      {% for r in rows %}
      <tr>
        <td><b>{{r.worker}}</b></td><td>{{r.brand}}</td>
        <td><div style="display:flex;gap:8px;align-items:center">
          <div class="seg">
            <div class="d" style="width:{{ (100*r.uploaded/r.assigned)|int if r.assigned else 0 }}%"></div>
            <div class="w" style="width:{{ (100*r.downloaded/r.assigned)|int if r.assigned else 0 }}%"></div>
          </div>
          <span class="num muted">{{r.uploaded}}/{{r.assigned}}</span></div></td>
        <td>{% if r.remaining==0 and r.assigned>0 %}<span class="pill done">done</span>
            {% else %}<span class="pill rem">{{r.remaining}} left</span>{% endif %}</td>
        <td><a href="{{url_for('assignment_view', aid=r.assignment_id)}}">details</a></td>
      </tr>
      {% endfor %}
    </table>{% endif %}
  </div>

  <div class="card">
    <h2>Workers</h2>
    <table>
      <tr><th>Name</th><th>Login</th><th>Role</th></tr>
      {% for w in workers_all %}<tr><td>{{w.name}}</td><td class="muted">{{w.login}}</td><td>{{w.role}}</td></tr>{% endfor %}
    </table>
    <form method="post" action="{{url_for('add_worker')}}" style="margin-top:16px">
      <div class="row">
        <div><label>Name</label><input name="name" placeholder="Yash" required></div>
        <div><label>Login</label><input name="login" placeholder="yash" required></div>
        <div><label>Password</label><input name="password" required></div>
        <div style="max-width:130px;flex:0"><button type="submit">Add worker</button></div>
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
      <tr><th>Brand</th><th>Progress</th><th>What's left</th></tr>
      {% for a in summary.assignments %}
      <tr>
        <td><b>{{a.brand}}</b></td>
        <td><div style="display:flex;gap:8px;align-items:center">
          <div class="seg">
            <div class="d" style="width:{{ (100*a.uploaded/a.assigned)|int if a.assigned else 0 }}%"></div>
            <div class="w" style="width:{{ (100*a.downloaded/a.assigned)|int if a.assigned else 0 }}%"></div>
          </div>
          <span class="num muted">{{a.uploaded}}/{{a.assigned}}</span></div></td>
        <td>{% if a.complete %}<span class="pill done">all done</span>
            {% else %}
              <a class="dlbtn" href="/me/download-zip/{{a.assignment_id}}">⬇ Download {{a.remaining}} photos (zip)</a>
              {% if a.downloaded %}<span class="pill rem" style="margin-left:6px">{{a.downloaded}} to retouch</span>{% endif %}
            {% endif %}</td>
      </tr>
      {% endfor %}
    </table>
    <p class="muted" style="font-size:12px;margin-top:14px">One zip with all your images + PSDs, organized in folders. Unzip it, retouch, then upload the finished files (upload button coming next). Big batches take a moment to zip.</p>
    {% endif %}
  </div>
</div>
<style>.dlbtn{display:inline-block;padding:7px 14px;border-radius:8px;background:var(--accent);
  color:#fff;font-size:13px;text-decoration:none}</style>
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


# ---- routes --------------------------------------------------------------
@app.route("/")
def home():
    if not session.get("uid"):
        return redirect(url_for("login"))
    return redirect(url_for("admin") if session.get("role") == ROLE_ADMIN
                    else url_for("me"))


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
    return render_template_string(
        ADMIN, name=session.get("name"),
        rows=engine.admin_overview(),
        workers=auth.list_workers(role="worker"),
        workers_all=auth.list_workers(),
        brands=brand_suggestions(),
        msgs=_pop_flash())


@app.route("/admin/assign", methods=["POST"])
@admin_required
def assign():
    worker = request.form.get("worker", "").strip()
    brand = request.form.get("brand", "").strip()
    count = (request.form.get("count") or "").strip()
    if worker and brand and count.isdigit() and int(count) > 0:
        res = engine.create_assignment(worker, brand, int(count))
        msg = f"Assigned {res['reserved']} photo(s) of {brand} to {res['worker']}."
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
    if name and login_ and pw:
        r = auth.create_user(name, login_, pw, role="worker")
        _flash(f"{'Updated' if r['updated'] else 'Added'} worker {r['name']} ({r['login']}).")
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
