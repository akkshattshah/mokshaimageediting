"""
webform.py  -  Small local web page for the admin to split photos among
freelancers, with background uploads.

Start it:   python webform.py
Then open:  http://127.0.0.1:5000

Pick a brand, type each freelancer's name and how many photos, then:
  - Preview   -> see the plan instantly, build/upload nothing.
  - Create    -> the page returns immediately and sends you to a live status
                 page. Zipping + Google Drive upload happen in the background;
                 each freelancer's share link appears as its upload finishes.
                 A freelancer's photos are marked "handed out" only once their
                 upload succeeds, so a failed upload never consumes photos.

Only one Create batch runs at a time (single admin), which keeps photo
allocation clean.
"""

import os
import threading
import time
import uuid
import datetime as dt

from flask import Flask, request, render_template_string, redirect, url_for

import distribute
import drive_upload
import download_s3

app = Flask(__name__)
ROWS = 8  # blank freelancer rows shown on the form
DELETE_AFTER_UPLOAD = True  # remove local photos + zip once safely in Drive

JOBS = {}                       # handout job_id -> job dict (in memory)
DLJOBS = {}                     # download job_id -> job dict
JOBS_LOCK = threading.Lock()

# Cached "how many new photos are waiting in S3" per brand. Checking S3 takes a
# few seconds, so it's done in a background thread and cached briefly; the page
# asks for it via /api/s3count so it never blocks loading the form.
S3_CACHE = {}
S3_TTL = 180        # seconds before we re-check
S3_MAX_DATES = 5    # only look at the newest few date folders (keeps it quick)


def _s3_refresh(brand):
    try:
        n = download_s3.count_new_photos(brand, max_dates=S3_MAX_DATES)
        S3_CACHE[brand] = {"count": n, "at": time.time(), "checking": False}
    except Exception as e:  # noqa
        prev = S3_CACHE.get(brand, {})
        S3_CACHE[brand] = {"count": prev.get("count"), "at": time.time(),
                           "checking": False, "error": str(e)}


def s3_count(brand):
    """Return the cached S3 availability for a brand, refreshing in the
    background when stale. Never blocks."""
    c = S3_CACHE.get(brand)
    now = time.time()
    stale = (c is None) or (now - c.get("at", 0) > S3_TTL)
    if stale and not (c or {}).get("checking"):
        prev = c or {}
        S3_CACHE[brand] = {"count": prev.get("count"),
                           "at": prev.get("at", 0), "checking": True}
        threading.Thread(target=_s3_refresh, args=(brand,), daemon=True).start()
    return S3_CACHE.get(brand, {"count": None, "checking": True, "at": 0})


BRANDS_CACHE = {"list": [], "at": 0.0}
BRANDS_TTL = 600      # brand folders change rarely


def local_brands():
    """Brands that currently have photos downloaded on this PC."""
    pool = distribute.POOL
    if not os.path.isdir(pool):
        return []
    return [d for d in sorted(os.listdir(pool))
            if os.path.isdir(os.path.join(pool, d)) and not d.startswith("_")]


def working_brands():
    """The brands you actually work with, read from brands.txt next to this
    script. Keeping the list here (rather than every brand in storage) keeps
    the dropdown short - edit brands.txt to add or remove one."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brands.txt")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def storage_brands():
    """Fallback only: every brand in S3 (cached). Used when brands.txt is
    missing, so the dropdown is never empty."""
    now = time.time()
    if now - BRANDS_CACHE["at"] > BRANDS_TTL:
        try:
            BRANDS_CACHE["list"] = download_s3.list_brands()
        except Exception:
            pass          # keep whatever we had; local brands still work
        BRANDS_CACHE["at"] = now
    return BRANDS_CACHE["list"]


def brands_with_counts():
    """Everything selectable: brands with photos here, plus brands in storage.
    Ones with photos ready are listed first. Returns (ordered_list, counts)."""
    have = set(local_brands())
    ledger = distribute.load_ledger()          # read once, not per brand
    # Brands you actually work with = ones with photos here, plus ones you've
    # handed out before (so a fully-delivered brand stays selectable and can be
    # re-downloaded). On a fresh PC with neither, fall back to brands.txt.
    used = {v.get("brand") for v in ledger.values() if v.get("brand")}
    allb = have | used
    if not allb:
        allb = set(working_brands()) or set(storage_brands())
    counts = {}
    for b in allb:
        if b in have:
            groups = distribute.scan_pool_photos(b)
            counts[b] = sum(1 for pid in groups if pid not in ledger)
        else:
            counts[b] = 0                      # nothing downloaded yet
    ready = sorted([b for b in allb if counts[b] > 0])
    empty = sorted([b for b in allb if counts[b] == 0])
    return ready + empty, counts


def parse_splits(form):
    names = form.getlist("name")
    counts = form.getlist("count")
    splits = []
    for n, c in zip(names, counts):
        n = (n or "").strip()
        c = (c or "").strip()
        if n and c.isdigit() and int(c) > 0:
            splits.append((n, int(c)))
    return splits


def running_job_id():
    for jid, job in JOBS.items():
        if job["status"] == "running":
            return jid
    return None


def running_dl_id():
    for jid, job in DLJOBS.items():
        if job["status"] in ("starting", "running"):
            return jid
    return None


def _dl_worker(job_id):
    """Background: fetch new photos from S3 into the pool."""
    job = DLJOBS[job_id]

    def prog(done, total, done_bytes, total_bytes):
        job["done"] = done
        job["total"] = total
        job["bytes"] = done_bytes
        job["total_bytes"] = total_bytes

    try:
        job["status"] = "running"
        res = download_s3.download_new_photos(
            job["brand"], job["count"], progress=prog)
        job.update(res)
        job["status"] = "done"
        S3_CACHE.pop(job["brand"], None)   # force a fresh availability check
    except Exception as e:  # noqa
        job["status"] = "error"
        job["error"] = str(e)


def _worker(job_id):
    """Background: zip + upload each freelancer, commit photos on success."""
    job = JOBS[job_id]
    brand = job["brand"]
    groups = distribute.scan_pool_photos(brand)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for item in job["items"]:
        if item["given"] == 0:
            item["status"] = "empty"
            continue
        try:
            item["status"] = "zipping"
            src_files = [f for pid in item["photo_ids"] for f in groups.get(pid, [])]
            zname, zpath = distribute.build_zip(
                brand, item["freelancer"], item["photo_ids"], stamp, groups)
            item["zip_name"] = zname
            item["status"] = "uploading"
            item["link"] = drive_upload.upload_and_share(zpath)
            distribute.commit_results(brand, [{
                "freelancer": item["freelancer"],
                "photo_ids": item["photo_ids"],
                "zip_name": zname,
                "stamp": stamp,
            }])
            distribute.append_handout({
                "when": dt.datetime.now().isoformat(timespec="seconds"),
                "stamp": stamp,
                "brand": brand,
                "freelancer": item["freelancer"],
                "photos": item["given"],
                "files": item["files"],
                "zip": zname,
                "link": item["link"],
            })
            # Now that it's safely in Drive, free up the operator's disk:
            # delete the delivered source photos and the local zip.
            if DELETE_AFTER_UPLOAD:
                distribute.purge_files(src_files)
                try:
                    os.remove(zpath)
                except OSError:
                    pass
            item["status"] = "done"
        except Exception as e:  # noqa
            item["status"] = "error"
            item["error"] = str(e)
    job["status"] = "done"


# ------------------------------- templates --------------------------------
PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<title>MokshaImage - Photo Handout</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;color:#1a1a1a}
  h1{font-size:20px} h2{font-size:16px;margin-top:28px}
  .card{border:1px solid #ddd;border-radius:10px;padding:18px 20px;margin:14px 0;background:#fafafa}
  label{font-size:13px;color:#444}
  select,input[type=text],input[type=number]{padding:7px 9px;border:1px solid #ccc;border-radius:7px;font-size:14px}
  input[type=text]{width:230px} input[type=number]{width:90px}
  .row{display:flex;gap:10px;align-items:center;margin:6px 0}
  .btns{margin-top:14px;display:flex;gap:10px}
  button{padding:9px 16px;border:0;border-radius:8px;font-size:14px;cursor:pointer}
  .preview{background:#e8e8e8} .create{background:#2b6cff;color:#fff}
  table{border-collapse:collapse;width:100%;margin-top:8px}
  td,th{border:1px solid #e0e0e0;padding:8px 10px;font-size:14px;text-align:left}
  .warn{color:#a12} .muted{color:#777;font-size:13px}
</style></head><body>
<h1>Photo Handout</h1>
<div style="margin:-6px 0 10px"><a href="{{ url_for('history') }}">📜 View handout history →</a></div>

{% if busy %}
  <div class="card">A handout batch is currently running.
    <a href="{{ url_for('status', job_id=busy) }}">View its progress →</a></div>
{% endif %}

{% if not brands %}
  <div class="card warn">No brands in the pool yet. Download some first, e.g.
  <code>python download_s3.py catchall_ireland --photos 20</code></div>
{% else %}
<form method="post" action="/run">
  <div class="card">
    <div class="row">
      <label>Brand:</label>
      <select name="brand">
        {% for b in brands %}
        <option value="{{b}}" {{'selected' if b==sel_brand else ''}}>{{b}} — {{counts[b]}} photos available</option>
        {% endfor %}
      </select>
    </div>
    <div class="muted">A “photo” = the image + its PSD, zipped together and counted as 1.</div>
    <div class="row" style="margin-top:12px;padding-top:12px;border-top:1px dashed #ddd">
      <span id="s3info" class="muted">checking storage for new photos…</span>
    </div>
    <div class="row">
      <input type="number" id="dlcount" value="100" min="1" title="how many new photos to fetch">
      <button type="button" class="preview" onclick="startDownload()">Download more</button>
      <span class="muted">fetches new photos from storage into the pool</span>
    </div>
    <h2>Freelancers</h2>
    {% for i in range(rows) %}
    <div class="row">
      <input type="text" name="name" placeholder="name (e.g. Vansh)"
             value="{{ prefill_names[i] if i < prefill_names|length else '' }}">
      <input type="number" name="count" min="0" placeholder="photos"
             value="{{ prefill_counts[i] if i < prefill_counts|length else '' }}">
    </div>
    {% endfor %}
    <div class="btns">
      <button class="preview" name="action" value="preview">Preview</button>
      <button class="create" name="action" value="create">Create &amp; Upload</button>
    </div>
    {% if not drive_ready %}
    <div class="muted warn" style="margin-top:10px">
      Google Drive not authorized yet — Preview works, but Create can’t upload
      until you run <code>python drive_auth.py</code> once.
    </div>
    {% endif %}
  </div>
</form>
{% endif %}

{% if summary %}
  <div class="card">
    <h2>Preview (nothing created) — {{ summary.brand }}</h2>
    <div class="muted">{{ summary.available_before }} photos available.</div>
    <table>
      <tr><th>Freelancer</th><th>Photos</th><th>Files</th><th>Zip (prospective)</th></tr>
      {% for r in summary.results %}
      <tr>
        <td>{{ r.freelancer }}</td>
        <td>{{ r.given }}/{{ r.requested }}{% if r.given < r.requested %} <span class="warn">short</span>{% endif %}</td>
        <td>{{ r.files }}</td>
        <td>{{ r.zip_name or '—' }}</td>
      </tr>
      {% endfor %}
    </table>
    <div class="muted" style="margin-top:8px">Leftover in pool: {{ summary.leftover }}
      {% if summary.shortfall %} · <span class="warn">short by {{ summary.shortfall }}</span>{% endif %}
    </div>
  </div>
{% endif %}

<script>
function selBrand(){var s=document.querySelector('select[name=brand]');return s?s.value:'';}
function refreshS3(){
  var b=selBrand(), el=document.getElementById('s3info');
  if(!b||!el) return;
  fetch('/api/s3count?brand='+encodeURIComponent(b))
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.error){ el.textContent='could not check storage: '+d.error; return; }
      if(d.count===null||d.count===undefined){
        el.textContent='checking storage for new photos…';
        setTimeout(refreshS3,2000); return;
      }
      el.textContent = d.count + ' more available to download from storage'
                     + ' (newest ' + d.max_dates + ' days)'
                     + (d.checking?' · refreshing…':'');
      if(d.checking) setTimeout(refreshS3,2500);
    })
    .catch(function(){ el.textContent='could not check storage'; });
}
function startDownload(){
  var b=selBrand(), n=document.getElementById('dlcount').value;
  if(!b||!n){ alert('Pick a brand and a number.'); return; }
  var fd=new FormData(); fd.append('brand',b); fd.append('count',n);
  fetch('/download',{method:'POST',body:fd})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.job_id){ window.location='/dlstatus/'+d.job_id; }
      else { alert(d.error||'could not start download'); }
    })
    .catch(function(){ alert('could not start download'); });
}
document.addEventListener('DOMContentLoaded',function(){
  refreshS3();
  var s=document.querySelector('select[name=brand]');
  if(s) s.addEventListener('change',refreshS3);
});
</script>
</body></html>
"""

STATUS = """
<!doctype html><html><head><meta charset="utf-8">
<title>Handout progress</title>
{% if job.status == 'running' %}<meta http-equiv="refresh" content="3">{% endif %}
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:820px;margin:32px auto;padding:0 16px;color:#1a1a1a}
  h1{font-size:20px}
  .card{border:1px solid #ddd;border-radius:10px;padding:18px 20px;margin:14px 0;background:#fafafa}
  table{border-collapse:collapse;width:100%} td,th{border:1px solid #e0e0e0;padding:8px 10px;font-size:14px;text-align:left}
  a.link{word-break:break-all}
  .s-done{color:#137a2b;font-weight:600} .s-uploading{color:#b26a00} .s-zipping{color:#7a5cc7}
  .s-error{color:#a12;font-weight:600} .s-pending,.s-empty{color:#777}
  .muted{color:#777;font-size:13px} .bar{margin:6px 0 14px}
</style></head><body>
<h1>Handout progress — {{ job.brand }}</h1>
<div class="card">
  <div class="bar">
    {% if job.status == 'running' %}
      <b>Uploading…</b> {{ done }}/{{ total }} done — this page refreshes every 3s.
    {% else %}
      <b>Finished.</b> {{ done }}/{{ total }} delivered.
    {% endif %}
  </div>
  <table>
    <tr><th>Freelancer</th><th>Photos</th><th>Status</th><th>Share link</th></tr>
    {% for it in job['items'] %}
    <tr>
      <td>{{ it.freelancer }}</td>
      <td>{{ it.given }}/{{ it.requested }}</td>
      <td class="s-{{ it.status }}">{{ it.status }}</td>
      <td>
        {% if it.link %}<a class="link" href="{{ it.link }}" target="_blank">{{ it.link }}</a>
        {% elif it.error %}<span class="s-error">{{ it.error }}</span>
        {% else %}—{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  <div class="muted" style="margin-top:10px">Leftover in pool: {{ job.leftover }}
    {% if job.shortfall %} · short by {{ job.shortfall }}{% endif %}</div>
</div>
<a href="{{ url_for('index') }}">← back to form</a>
</body></html>
"""


DLSTATUS = """
<!doctype html><html><head><meta charset="utf-8">
<title>Downloading photos</title>
{% if job.status in ['starting','running'] %}<meta http-equiv="refresh" content="2">{% endif %}
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;color:#1a1a1a}
  h1{font-size:20px}
  .card{border:1px solid #ddd;border-radius:10px;padding:18px 20px;margin:14px 0;background:#fafafa}
  .track{height:14px;background:#e6e6e6;border-radius:7px;overflow:hidden;margin:10px 0}
  .fill{height:100%;background:#2b6cff;width:{{pct}}%}
  .muted{color:#777;font-size:13px} .warn{color:#a12;font-weight:600}
  .ok{color:#137a2b;font-weight:600}
</style></head><body>
<h1>Downloading photos — {{ job.brand }}</h1>
<div class="card">
  {% if job.status in ['starting','running'] %}
    <b>Downloading…</b> {{ job.done }} / {{ job.total }} files
    <div class="track"><div class="fill"></div></div>
    <div class="muted">{{ human(job.bytes) }} of {{ human(job.total_bytes) }} ·
      this page refreshes every 2s. You can leave it running.</div>
    {% if job.total == 0 %}<div class="muted">Looking up what's new in storage…</div>{% endif %}
  {% elif job.status == 'done' %}
    <div class="ok">Finished — {{ job.photos }} photo(s) downloaded
      ({{ job.files }} files, {{ human(job.bytes if job.bytes else job.total_bytes) }}).</div>
    {% if job.errors %}<div class="warn">{{ job.errors }} file(s) failed.</div>{% endif %}
    <div class="muted" style="margin-top:8px">They're now available to hand out.</div>
  {% else %}
    <div class="warn">Download failed: {{ job.error }}</div>
  {% endif %}
</div>
<a href="{{ url_for('index') }}">← back to form</a>
</body></html>
"""

HISTORY = """
<!doctype html><html><head><meta charset="utf-8">
<title>Handout history</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:960px;margin:32px auto;padding:0 16px;color:#1a1a1a}
  h1{font-size:20px}
  .card{border:1px solid #ddd;border-radius:10px;padding:14px 18px;margin:14px 0;background:#fafafa}
  table{border-collapse:collapse;width:100%} td,th{border:1px solid #e0e0e0;padding:8px 10px;font-size:14px;text-align:left;vertical-align:top}
  th{background:#f0f0f0} a.link{word-break:break-all}
  .muted{color:#777;font-size:13px} .num{text-align:right}
  input#q{padding:7px 9px;border:1px solid #ccc;border-radius:7px;font-size:14px;width:260px}
</style></head><body>
<h1>Handout history</h1>
<div style="margin:-6px 0 12px"><a href="{{ url_for('index') }}">← back to form</a></div>

{% if not rows %}
  <div class="card muted">No handouts recorded yet. They’ll appear here after you Create &amp; Upload.</div>
{% else %}
<div class="card">
  <div class="muted">{{ rows|length }} deliveries · {{ total_photos }} photos handed out in total.
     Type to filter:</div>
  <input id="q" placeholder="filter by name, brand or date…" onkeyup="filt()">
  <table id="t">
    <tr><th>Date &amp; time</th><th>Brand</th><th>Freelancer</th><th class="num">Photos</th><th>Drive link</th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.display_when }}</td>
      <td>{{ r.brand }}</td>
      <td>{{ r.freelancer }}</td>
      <td class="num">{{ r.photos }}</td>
      <td>{% if r.link %}<a class="link" href="{{ r.link }}" target="_blank">open</a>
          <div class="muted">{{ r.link }}</div>{% else %}—{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
</div>
<script>
function filt(){var q=document.getElementById('q').value.toLowerCase();
 var rs=document.querySelectorAll('#t tr');for(var i=1;i<rs.length;i++){
  rs[i].style.display=rs[i].innerText.toLowerCase().indexOf(q)>-1?'':'none';}}
</script>
{% endif %}
</body></html>
"""


def _display_when(rec):
    s = rec.get("stamp", "")
    if len(s) >= 15 and "-" in s:
        d, t = s.split("-", 1)
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}"
    return rec.get("when", s)


@app.route("/history")
def history():
    recs = distribute.load_handouts()
    for r in recs:
        r["display_when"] = _display_when(r)
    recs.sort(key=lambda r: r.get("stamp", r.get("when", "")), reverse=True)
    total_photos = sum(r.get("photos", 0) for r in recs)
    return render_template_string(HISTORY, rows=recs, total_photos=total_photos)


@app.route("/")
def index():
    bs, counts = brands_with_counts()
    return render_template_string(
        PAGE, brands=bs, counts=counts, rows=ROWS,
        sel_brand=bs[0] if bs else None, summary=None,
        drive_ready=drive_upload.drive_ready(),
        prefill_names=[], prefill_counts=[], busy=running_job_id())


@app.route("/run", methods=["POST"])
def run():
    bs, counts = brands_with_counts()
    brand = request.form.get("brand")
    action = request.form.get("action", "preview")
    splits = parse_splits(request.form)

    if action == "preview":
        summary = distribute.assign(brand, splits, dry_run=True) if (brand and splits) else None
        return render_template_string(
            PAGE, brands=bs, counts=counts, rows=ROWS,
            sel_brand=brand, summary=summary,
            drive_ready=drive_upload.drive_ready(),
            prefill_names=[n for n, _ in splits],
            prefill_counts=[c for _, c in splits], busy=running_job_id())

    # ---- create: plan instantly, run zip+upload in the background ----
    if not (brand and splits):
        return redirect(url_for("index"))
    busy = running_job_id()
    if busy:
        return redirect(url_for("status", job_id=busy))

    plan = distribute.assign(brand, splits, dry_run=True)  # allocation only
    jid = uuid.uuid4().hex[:8]
    job = {
        "id": jid, "brand": brand, "status": "running",
        "available_before": plan["available_before"],
        "leftover": plan["leftover"], "shortfall": plan["shortfall"],
        "items": [{
            "freelancer": r["freelancer"], "requested": r["requested"],
            "given": r["given"], "files": r["files"],
            "photo_ids": r["photo_ids"], "zip_name": None,
            "status": "pending", "link": None, "error": None,
        } for r in plan["results"]],
    }
    with JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=_worker, args=(jid,), daemon=True).start()
    return redirect(url_for("status", job_id=jid))


@app.route("/api/s3count")
def api_s3count():
    brand = request.args.get("brand", "")
    if not brand:
        return {"count": None, "checking": False, "error": "no brand"}
    info = s3_count(brand)
    return {"count": info.get("count"), "checking": bool(info.get("checking")),
            "error": info.get("error"), "max_dates": S3_MAX_DATES}


@app.route("/download", methods=["POST"])
def download():
    brand = request.form.get("brand", "")
    count = (request.form.get("count") or "").strip()
    if not brand or not count.isdigit() or int(count) <= 0:
        return {"error": "Pick a brand and how many photos to download."}
    if running_job_id():
        return {"error": "A handout is still uploading - wait for it to finish."}
    busy = running_dl_id()
    if busy:
        return {"job_id": busy}
    jid = uuid.uuid4().hex[:8]
    DLJOBS[jid] = {"id": jid, "brand": brand, "count": int(count),
                   "status": "starting", "done": 0, "total": 0,
                   "bytes": 0, "total_bytes": 0, "photos": 0, "errors": 0}
    threading.Thread(target=_dl_worker, args=(jid,), daemon=True).start()
    return {"job_id": jid}


@app.route("/dlstatus/<job_id>")
def dlstatus(job_id):
    job = DLJOBS.get(job_id)
    if not job:
        return redirect(url_for("index"))
    pct = 0
    if job.get("total"):
        pct = int(job["done"] * 100 / job["total"])
    return render_template_string(DLSTATUS, job=job, pct=pct,
                                  human=distribute_human)


def distribute_human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return redirect(url_for("index"))
    total = sum(1 for it in job["items"] if it["given"] > 0)
    done = sum(1 for it in job["items"] if it["status"] == "done")
    return render_template_string(STATUS, job=job, total=total, done=done)


def _open_browser_when_ready(url):
    """Wait for the server to answer, then open the default browser once.
    Only used when launched via the double-click launcher (HANDOUT_OPEN=1)."""
    import time
    import webbrowser
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    if os.environ.get("HANDOUT_OPEN") == "1":
        threading.Thread(
            target=_open_browser_when_ready,
            args=("http://127.0.0.1:5000/",), daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
