"""Offline regression tests for the Assign dropdown's "N available" counts
(engine.brand_availability) and the photos create_assignment can actually
reserve.

Fully isolated - no network, no real S3, no real database:
  * boto3 is swapped for a stub in sys.modules BEFORE hub is imported, so
    hub.s3source talks to an in-memory fake bucket (FAKE_S3).
  * DATABASE_URL is forced to a throwaway SQLite file in a temp dir BEFORE
    hub.db is imported, and checked afterwards.
hub.app (Drive, auth seeding) is never imported.

Run on its own:   python -m pytest tests/test_brand_availability.py -v

Tests named test_characterize_* pin down current behaviour whose correctness
depends on what is really in the client's bucket - they are not claims that
the behaviour is right or wrong.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types

import pytest

if any(m == "hub" or m.startswith("hub.") for m in sys.modules):
    raise RuntimeError("hub was imported before test isolation was set up")

_TMP = tempfile.mkdtemp(prefix="hub-avail-test-")
_DB_FILE = os.path.join(_TMP, "test.db").replace("\\", "/")
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_FILE


class FakeS3:
    """Just enough of list_objects_v2 (Prefix + Delimiter) to back s3source.
    Like real S3, one call returns at most MaxKeys (1000) keys + common
    prefixes, in key order, and the paginator follows ContinuationToken."""

    def __init__(self):
        self.keys = set()
        self.fail = False

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None,
                        MaxKeys=1000, ContinuationToken=None, **_):
        if self.fail:
            raise RuntimeError("simulated S3 outage")
        entries, seen = [], set()
        for k in sorted(self.keys):
            if not k.startswith(Prefix):
                continue
            rest = k[len(Prefix):]
            if Delimiter and Delimiter in rest:
                p = Prefix + rest.split(Delimiter)[0] + Delimiter
                if p not in seen:
                    seen.add(p)
                    entries.append(("prefix", p))
            else:
                entries.append(("key", k))
        start = int(ContinuationToken or 0)
        page = entries[start:start + MaxKeys]
        out = {"IsTruncated": start + MaxKeys < len(entries)}
        if out["IsTruncated"]:
            out["NextContinuationToken"] = str(start + MaxKeys)
        contents = [{"Key": v, "Size": 1} for t, v in page if t == "key"]
        prefixes = [{"Prefix": v} for t, v in page if t == "prefix"]
        if contents:
            out["Contents"] = contents
        if prefixes:
            out["CommonPrefixes"] = prefixes
        return out

    def get_paginator(self, _name):
        s3 = self

        class _Pager:
            def paginate(self, **kw):
                token = None
                while True:
                    page = s3.list_objects_v2(ContinuationToken=token, **kw)
                    yield page
                    if not page["IsTruncated"]:
                        return
                    token = page["NextContinuationToken"]

        return _Pager()


FAKE_S3 = FakeS3()
_real_boto3 = sys.modules.get("boto3")
_stub = types.ModuleType("boto3")
_stub.client = lambda *a, **k: FAKE_S3
sys.modules["boto3"] = _stub
try:
    from hub import db, engine, s3source               # noqa: E402
    from hub.models import (Worker, Assignment, AssignmentPhoto,   # noqa: E402
                            ROLE_ADMIN, ST_UPLOADED)
finally:
    if _real_boto3 is not None:
        sys.modules["boto3"] = _real_boto3
    else:
        del sys.modules["boto3"]

assert db.DB_URL == "sqlite:///" + _DB_FILE, db.DB_URL
assert s3source._s3 is FAKE_S3
db.init_db()

BASE = s3source.BASE


def teardown_module():
    db.engine.dispose()
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_state():
    FAKE_S3.keys.clear()
    FAKE_S3.fail = False
    engine._windows.clear()
    with db.engine.begin() as c:
        for t in reversed(db.Base.metadata.sorted_tables):
            c.execute(t.delete())
    yield


# ---- helpers -------------------------------------------------------------
def put(*rel_keys):
    FAKE_S3.keys.update(BASE + k for k in rel_keys)


def put_pairs(brand, date, n, start=0, sub="ext"):
    """n photos, each the usual .psd + _orig.jpg pair."""
    for i in range(start, start + n):
        put(f"{brand}/{date}/{sub}/car-{i:03d}.psd",
            f"{brand}/{date}/{sub}/car-{i:03d}_orig.jpg")


def shown(*brands):
    """What the dropdown would show right now (fresh S3 listing)."""
    engine._windows.clear()
    counts, _ = engine.brand_availability(list(brands))
    return counts if len(brands) > 1 else counts[brands[0]]


def assignable(brand):
    """How many photos create_assignment could still reserve right now."""
    with db.SessionLocal() as s:
        taken = engine.claimed_photo_ids(s, brand)
    return len(s3source.available_photo_ids(brand, taken))


def make_admin():
    with db.SessionLocal() as s:
        a = Worker(name="Admin", login="admin", role=ROLE_ADMIN)
        s.add(a)
        s.commit()
        return a.id


# ---- filename pairing ----------------------------------------------------
def test_pair_of_psd_and_orig_counts_as_one_photo():
    put("b/20260701/x/a.psd", "b/20260701/x/a_orig.jpg",
        "b/20260701/x/c.psd", "b/20260701/x/c_orig.jpeg",
        "b/20260701/x/d.PSD", "b/20260701/x/d_ORIG.JPG")
    assert s3source.folder_photo_ids("b", "20260701") == {
        "b/20260701/x/a", "b/20260701/x/c", "b/20260701/x/d"}
    assert shown("b") == assignable("b") == 3


def test_characterize_name_case_mismatch_counts_twice():
    put("b/20260701/x/ABC.psd", "b/20260701/x/abc_orig.jpg")
    assert shown("b") == assignable("b") == 2


def test_characterize_extra_image_variant_counts_as_its_own_photo():
    put("b/20260701/x/a.psd", "b/20260701/x/a_orig.jpg",
        "b/20260701/x/a_mask.png")
    assert shown("b") == assignable("b") == 2


def test_characterize_lone_file_counts_as_a_photo():
    put("b/20260701/x/a_orig.jpg", "b/20260701/x/z.psd")
    assert shown("b") == assignable("b") == 2


# ---- what's in a folder --------------------------------------------------
def test_non_image_files_and_folder_markers_are_ignored():
    put("b/20260701/x/a.psd", "b/20260701/x/a_orig.jpg",
        "b/20260701/x/", "b/20260701/x/notes.txt", "b/20260701/x/meta.json")
    assert shown("b") == assignable("b") == 1


def test_characterize_copy_in_a_subfolder_counts_separately():
    put("b/20260701/x/a.psd", "b/20260701/x/a_orig.jpg",
        "b/20260701/x/done/a.psd")
    assert shown("b") == assignable("b") == 2


# ---- date-folder window --------------------------------------------------
def test_window_is_the_newest_eight_date_folders():
    for day in range(1, 11):                      # 10 folders, 1 photo each
        put_pairs("b", f"202607{day:02d}", 1)
    assert s3source.date_folders("b")[:s3source.MAX_DATES][-1] == "20260703"
    assert shown("b") == assignable("b") == 8


def test_non_date_folders_never_enter_the_window():
    """Date folders are YYYYMMDD. Anything else sorted ahead of every real
    date and took a slot (and its photos) in the newest-8 window."""
    for day in range(1, 9):
        put_pairs("b", f"202607{day:02d}", 1)
    for junk in ("archive", "tmp", "zz_old", "20261399", "2026071",
                 "202607010"):
        put_pairs("b", junk, 5)
    assert s3source.date_folders("b") == [
        f"202607{day:02d}" for day in range(8, 0, -1)]
    assert shown("b") == assignable("b") == 8


def test_date_folders_with_a_suffix_are_kept_in_date_order():
    """A second delivery on the same day (20260716_2) is still a date folder,
    and sorts as the newest of that day."""
    for d in ("20260714", "20260716", "20260716_2", "20260716-b",
              "20260715_retry", "20260717"):
        put_pairs("b", d, 1)
    for junk in ("20261399_2", "archive_20260716", "20260716_", "20260716 copy",
                 "202607161", "tmp_2"):
        put_pairs("b", junk, 5)
    assert s3source.date_folders("b") == [
        "20260717", "20260716_2", "20260716-b", "20260716",
        "20260715_retry", "20260714"]
    assert shown("b") == assignable("b") == 6


def test_window_uses_newest_dates_beyond_the_first_1000_folders():
    """S3 returns at most 1000 folders per call, oldest first, so a single
    unpaginated call would build the 'newest' window from old folders."""
    import datetime as dt
    first = dt.date(2023, 1, 1)
    days = [(first + dt.timedelta(days=i)).strftime("%Y%m%d")
            for i in range(1005)]
    for d in days:
        put(f"b/{d}/x/p.psd")
    assert s3source.date_folders("b")[:s3source.MAX_DATES] == days[::-1][:8]
    with db.SessionLocal() as s:
        taken = engine.claimed_photo_ids(s, "b")
    picked = s3source.available_photo_ids("b", taken, want=1)
    assert picked == [f"b/{days[-1]}/x/p"]
    assert shown("b") == 8


def test_brands_sharing_a_prefix_are_counted_separately():
    put_pairs("hil", "20260701", 3)
    put_pairs("hil_hil", "20260701", 5)
    engine.create_assignment("w1", "hil_hil", 2)
    assert shown("hil", "hil_hil") == {"hil": 3, "hil_hil": 3}
    assert assignable("hil") == 3 and assignable("hil_hil") == 3


# ---- claimed photos ------------------------------------------------------
def test_assignment_lowers_the_count_by_what_it_reserved():
    put_pairs("catchall_ireland", "20260701", 10)
    res = engine.create_assignment("w1", "catchall_ireland", 4)
    assert res["reserved"] == 4
    assert shown("catchall_ireland") == assignable("catchall_ireland") == 6


def test_over_asking_reserves_only_what_is_shown():
    put_pairs("b", "20260701", 5)
    before = shown("b")
    res = engine.create_assignment("w1", "b", 50)
    assert res["reserved"] == before == 5 and res["short"] == 45
    assert shown("b") == assignable("b") == 0


def test_claims_that_fell_out_of_the_window_are_not_subtracted():
    put_pairs("b", "20260601", 3)
    engine.create_assignment("w1", "b", 2)        # from the old folder
    for day in range(1, 9):                       # 8 newer folders push it out
        put_pairs("b", f"202607{day:02d}", 1)
    assert shown("b") == assignable("b") == 8


def test_reassign_and_remove_person_keep_count_consistent():
    admin = make_admin()
    put_pairs("b", "20260701", 10)
    res = engine.create_assignment("w1", "b", 6, owner_id=admin)
    engine.mark_uploaded(res["assignment_id"], res["photo_ids"][:2])
    engine.reassign_remaining(res["assignment_id"], "w2")
    assert shown("b") == assignable("b") == 4     # moved, not released
    with db.SessionLocal() as s:
        w2 = s.query(Worker).filter_by(login="w2").one().id
    engine.remove_person(admin, w2)               # frees w2's 4 unfinished
    assert shown("b") == assignable("b") == 8


def test_count_matches_assignable_when_a_photo_is_claimed_twice():
    """Two batches can hold the same photo (see the race test below). The
    photo is gone from the pool once, so it must be subtracted once."""
    put_pairs("b", "20260701", 10)
    res = engine.create_assignment("w1", "b", 3)
    dup = res["photo_ids"][0]
    with db.SessionLocal() as s:
        w = s.query(Worker).filter_by(login="w1").one()
        a = Assignment(worker_id=w.id, brand="b", count=1)
        s.add(a)
        s.flush()
        s.add(AssignmentPhoto(assignment_id=a.id, photo_id=dup))
        s.commit()
    assert assignable("b") == 7
    assert shown("b") == assignable("b")


def test_count_matches_assignable_when_claimed_files_left_s3():
    """A claimed photo whose files are no longer in the bucket isn't in the
    pool any more, so it can't be subtracted from it a second time."""
    put_pairs("b", "20260701", 10)
    res = engine.create_assignment("w1", "b", 3)
    for pid in res["photo_ids"][:2]:
        FAKE_S3.keys -= {BASE + pid + ".psd", BASE + pid + "_orig.jpg"}
    assert assignable("b") == 7
    assert shown("b") == assignable("b")


@pytest.mark.parametrize("n_photos,assign", [(0, []), (7, [3]), (25, [5, 5, 20]),
                                              (12, [12, 1])])
def test_count_always_matches_assignable(n_photos, assign):
    put_pairs("b", "20260701", n_photos)
    for i, n in enumerate(assign):
        engine.create_assignment(f"w{i}", "b", n)
        assert shown("b") == assignable("b")


# ---- the only paths where the count can exceed what Assign can reserve -----
def test_characterize_cached_listing_can_overstate_for_up_to_ttl():
    put_pairs("b", "20260701", 10)
    engine.brand_availability(["b"])              # cache the listing
    FAKE_S3.keys = {k for k in FAKE_S3.keys if "car-00" not in k}  # 10 -> 0 left
    counts, stale = engine.brand_availability(["b"])   # within the 600 s TTL
    assert counts["b"] == 10 and not stale
    assert assignable("b") == 0


def _expire(brand):
    listed, ds, rest = engine._windows[brand]
    engine._windows[brand] = (listed - engine._WINDOW_TTL - 1, ds, rest)


def test_failed_relist_does_not_present_old_count_as_current():
    put_pairs("b", "20260701", 10)
    engine.brand_availability(["b"])
    _expire("b")
    FAKE_S3.keys.clear()
    FAKE_S3.fail = True                           # S3 errors on the relist
    counts, stale = engine.brand_availability(["b"])
    assert counts["b"] is None and stale          # unknown, not "10 available"
    FAKE_S3.fail = False                          # recovers on the next try
    counts, stale = engine.brand_availability(["b"])
    assert counts["b"] == 0 and not stale


def test_page_render_does_not_show_an_expired_count():
    """refresh=False (the server-rendered page) must say 'counting…' for an
    expired listing, so the page fetches a fresh one, not print the old one."""
    put_pairs("b", "20260701", 10)
    engine.brand_availability(["b"])
    counts, stale = engine.brand_availability(["b"], refresh=False)
    assert counts["b"] == 10 and not stale        # fresh cache: shown at once
    _expire("b")
    counts, stale = engine.brand_availability(["b"], refresh=False)
    assert counts["b"] is None and stale


def test_one_failing_brand_does_not_blank_the_others():
    put_pairs("a", "20260701", 2)
    put_pairs("b", "20260701", 3)
    engine.brand_availability(["a", "b"])
    _expire("a")
    real = s3source.date_folders

    def flaky(brand):
        if brand == "a":
            raise RuntimeError("simulated S3 error")
        return real(brand)

    s3source.date_folders = flaky
    try:
        counts, stale = engine.brand_availability(["a", "b"])
    finally:
        s3source.date_folders = real
    assert counts == {"a": None, "b": 3} and stale


# ---- completed photos (finished JPG in upload-retouching-jpg/) ------------
def finish(brand, date, *names, ext=".jpg"):
    """The client's pipeline stores a finished photo as a flat JPG."""
    FAKE_S3.keys.update(f"{s3source.DONE_BASE}{brand}/{date}/{n}{ext}"
                        for n in names)


def test_completed_photos_are_not_available():
    put_pairs("b", "20260701", 10)                # car-000 .. car-009
    finish("b", "20260701", "car-000", "car-001", "car-002")
    assert shown("b") == assignable("b") == 7
    res = engine.create_assignment("w1", "b", 50)
    assert res["reserved"] == 7
    assert not {p.rsplit("/", 1)[-1] for p in res["photo_ids"]} & {
        "car-000", "car-001", "car-002"}


def test_pending_unassigned_and_assigned_are_counted_correctly():
    put_pairs("b", "20260701", 10)
    finish("b", "20260701", "car-009")            # completed
    engine.create_assignment("w1", "b", 4)        # pending, assigned
    assert shown("b") == assignable("b") == 5     # pending, unassigned


def test_count_drops_when_a_photo_is_completed():
    put_pairs("b", "20260701", 10)
    assert shown("b") == 10
    finish("b", "20260701", "car-004")
    assert shown("b") == assignable("b") == 9
    finish("b", "20260701", "car-005", "car-006", ext=".JPG")
    assert shown("b") == assignable("b") == 7


def test_completion_only_counts_in_the_same_brand_and_date():
    put_pairs("b", "20260701", 3)
    put_pairs("b", "20260702", 3)
    put_pairs("other", "20260701", 3)
    finish("b", "20260702", "car-000")            # a different date folder
    finish("other", "20260701", "car-001")        # a different brand
    assert shown("b") == assignable("b") == 5     # only b/20260702/car-000 done


def test_completed_photo_with_nested_download_path():
    """Real layout: download .../<date>/<view>/<id>/<name>.psd, finished JPG
    flat at upload-retouching-jpg/<brand>/<date>/<name>.jpg"""
    put("hil_hil/20261021/exterior-1/123-45/abc-v4.psd",
        "hil_hil/20261021/exterior-1/123-45/abc-v4_orig.jpeg",
        "hil_hil/20261021/exterior-1/123-46/abd-v4.psd",
        "hil_hil/20261021/exterior-1/123-46/abd-v4_orig.jpeg")
    finish("hil_hil", "20261021", "abc-v4")
    assert shown("hil_hil") == assignable("hil_hil") == 1


def test_completion_listing_failure_is_not_shown_as_a_count(monkeypatch):
    put_pairs("b", "20260701", 3)

    def broken(*_a):
        raise RuntimeError("simulated S3 error on the finished-photo listing")

    monkeypatch.setattr(s3source, "finished_names", broken)
    engine._windows.clear()
    counts, stale = engine.brand_availability(["b"])
    assert counts["b"] is None and stale


def test_completion_check_can_be_disabled(monkeypatch):
    put_pairs("b", "20260701", 3)
    finish("b", "20260701", "car-000")
    monkeypatch.setattr(s3source, "DONE_BASE", "")
    assert shown("b") == assignable("b") == 3


# ---- known defects outside the count, documented not fixed ---------------
@pytest.mark.xfail(strict=True, reason="create_assignment has no lock or "
                   "unique constraint: two overlapping assigns can reserve "
                   "the same photo for two workers")
def test_overlapping_assignments_never_share_a_photo(monkeypatch):
    put_pairs("b", "20260701", 5)
    with db.SessionLocal() as s:      # existing workers: no write before listing
        engine.get_or_create_worker(s, "w1")
        engine.get_or_create_worker(s, "w2")
        s.commit()
    real = s3source.available_photo_ids
    inner = {}

    def slow_listing(brand, exclude, want=None, **kw):
        if not inner:      # a second admin assigns while we're listing S3
            inner["res"] = None
            inner["res"] = engine.create_assignment("w2", "b", 3)
        return real(brand, exclude, want=want, **kw)

    monkeypatch.setattr(s3source, "available_photo_ids", slow_listing)
    first = engine.create_assignment("w1", "b", 3)
    assert not set(first["photo_ids"]) & set(inner["res"]["photo_ids"])


def test_files_for_photo_returns_only_that_photos_files():
    """Listing by bare prefix made photo 'car-1' also pull car-10's files
    (another worker's photo) into the download."""
    put("b/20260701/x/car-1.psd", "b/20260701/x/car-1_orig.jpg",
        "b/20260701/x/car-10.psd", "b/20260701/x/car-10_orig.jpg",
        "b/20260701/x/car-1.5.psd", "b/20260701/x/car-1-v2_orig.jpeg")
    assert sorted(s3source.files_for_photo("b/20260701/x/car-1")) == [
        BASE + "b/20260701/x/car-1.psd", BASE + "b/20260701/x/car-1_orig.jpg"]


def test_files_for_photo_reads_past_the_first_1000_keys():
    """Prefix 'car-1' also matches car-10..car-1999 (2,222 keys here) and
    car-1_orig.jpg sorts after all of them, so a single unpaginated call
    dropped the photo's own original from the download."""
    for i in range(1, 2001):
        put(f"b/20260701/x/car-{i}.psd", f"b/20260701/x/car-{i}_orig.jpg")
    assert sorted(s3source.files_for_photo("b/20260701/x/car-1")) == [
        BASE + "b/20260701/x/car-1.psd", BASE + "b/20260701/x/car-1_orig.jpg"]
    assert shown("b") == assignable("b") == 2000


def test_webp_original_pairs_with_its_psd_and_downloads():
    """Real bucket: 24 photos are <name>.psd + <name>_orig.webp. The webp is
    the photo's original - it must download with the PSD and not count as a
    second photo."""
    put("b/20260701/x/car-1.psd", "b/20260701/x/car-1_orig.webp",
        "b/20260701/x/car-2.psd", "b/20260701/x/car-2_orig.jpeg")
    assert shown("b") == assignable("b") == 2
    assert sorted(s3source.files_for_photo("b/20260701/x/car-1")) == [
        BASE + "b/20260701/x/car-1.psd", BASE + "b/20260701/x/car-1_orig.webp"]


def test_documented_layout_nested_views_and_versions():
    """README/models document ids like
    catchall_ireland/20260716/exterior-1/.../<name>-v4 : nested view folders,
    .jpeg originals and version suffixes (-v4 vs -v40 must not mix)."""
    root = "catchall_ireland/20260716"
    for view in ("exterior-1", "interior-2"):
        for car in ("VIN123", "VIN124"):
            for v in ("v3", "v4", "v40"):
                put(f"{root}/{view}/{car}/{car}-{view}-{v}.psd",
                    f"{root}/{view}/{car}/{car}-{view}-{v}_orig.jpeg")
    assert shown("catchall_ireland") == assignable("catchall_ireland") == 12
    pid = f"{root}/exterior-1/VIN123/VIN123-exterior-1-v4"
    assert sorted(k.rsplit("/", 1)[1] for k in s3source.files_for_photo(pid)) == [
        "VIN123-exterior-1-v4.psd", "VIN123-exterior-1-v4_orig.jpeg"]


def test_photo_claimed_from_a_now_skipped_folder_still_downloads():
    """Batches reserved before the fix may hold photos from a non-date folder.
    They drop out of the count, but the worker can still download them."""
    with db.SessionLocal() as s:
        w = engine.get_or_create_worker(s, "w1")
        a = Assignment(worker_id=w.id, brand="b", count=1)
        s.add(a)
        s.flush()
        s.add(AssignmentPhoto(assignment_id=a.id, photo_id="b/archive/x/old"))
        s.commit()
    put("b/archive/x/old.psd", "b/archive/x/old_orig.jpg")
    put_pairs("b", "20260701", 2)
    assert shown("b") == assignable("b") == 2
    assert sorted(s3source.files_for_photo("b/archive/x/old")) == [
        BASE + "b/archive/x/old.psd", BASE + "b/archive/x/old_orig.jpg"]


def _audit_module():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts", "audit_brand_availability.py")
    spec = importlib.util.spec_from_file_location("audit_brand_availability", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_script_reconciles_every_source(tmp_path):
    """The operator audit must reproduce the dropdown (before and after the
    fix) from the same S3 listing + DB, and split it by legacy / SLA."""
    for day in range(1, 10):                      # 9 dated folders x 5
        put_pairs("b", f"202607{day:02d}", 5)
    put("b/20260709/ext/car-000_mask.png")
    put_pairs("b", "archive", 50)
    engine.create_assignment("w1", "b", 3)        # car-000, car-000_mask, car-001
    ledger = tmp_path / "_ledger.json"
    ledger.write_text(json.dumps({
        "b/20260708/ext/car-000": {}, "b/20260708/ext/car-001": {},
        "b/20260601/x/old": {}, "other/20260708/ext/car-000": {}}))
    pending = tmp_path / "pending.txt"
    pending.write_text("\n".join([
        "b/20260709/ext/car-003_orig.jpg",                    # exact, free
        BASE + "b/20260707/ext/car-004.psd",                  # full key, free
        "b/20260709/ext/car-001.psd",                         # exact, claimed
        "car-002",                                            # name in 8 folders
        "b/20260101/x/gone.psd"]))                            # not in window

    finish("b", "20260709", "car-004")             # one finished, unassigned
    out = _audit_module().audit(FAKE_S3, "b", db_url=db.DB_URL,
                                ledger_path=str(ledger), pending_path=str(pending))
    assert out["finished_in_window_now"] == 1
    assert out["pending_in_window_now"] == 40
    assert out["top_level_folders"]["not_date_folders"] == ["archive"]
    assert out["window_before_fix"][0] == "archive"
    assert out["photo_ids_in_window_now"] == 41
    assert out["dropdown_now"] == shown("b") == assignable("b") == 37
    assert out["dropdown_before_fix"] == 50 + 35 + 1 - 3
    assert out["hub_claims"]["distinct_photos"] == 3
    assert out["folders"]["20260709"]["ids_that_extend_another_id_with_underscore"] == 1
    assert out["legacy_ledger"] == {"brand_entries": 3, "in_window_now": 2,
                                    "in_window_and_not_claimed_by_hub": 2}
    assert out["available_after_excluding_legacy"] == 35
    assert out["sla_pending"] == {
        "lines": 5, "matched_in_window": 3, "ambiguous_names": 1,
        "not_in_window": 1, "pending_and_still_available": 2,
        "available_but_not_pending": 33}
    assert "car-" not in json.dumps(out)          # aggregates only, no names


def test_audit_script_only_lists_s3():
    class ListOnly:
        def __init__(self):
            self.calls = []

        def get_paginator(self, name):
            self.calls.append(name)
            return FAKE_S3.get_paginator(name)

        def __getattr__(self, name):
            raise AssertionError(f"audit called s3.{name}")

    put_pairs("b", "20260701", 2)
    s3 = ListOnly()
    _audit_module().audit(s3, "b")
    assert set(s3.calls) == {"list_objects_v2"}


def test_every_image_file_downloads_with_exactly_one_photo():
    put("b/20260701/x/car-1.psd", "b/20260701/x/car-1_orig.jpg",
        "b/20260701/x/car-10.PSD", "b/20260701/x/car-10_ORIG.JPEG",
        "b/20260701/x/car-1_mask.png", "b/20260701/x/car-2_orig.jpg",
        "b/20260701/x/notes.txt")
    owners = {}
    for pid in s3source.folder_photo_ids("b", "20260701"):
        for key in s3source.files_for_photo(pid):
            owners.setdefault(key, []).append(pid)
    images = {k for k in FAKE_S3.keys if k.lower().endswith(s3source.IMG_EXTS)}
    assert set(owners) == images
    assert all(len(p) == 1 for p in owners.values()), owners
