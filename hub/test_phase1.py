"""Phase 1 end-to-end check: run with  python -m hub.test_phase1
Exercises reserve -> download -> partial upload -> verify against real S3 + a
fresh SQLite DB."""

from .db import init_db
from . import engine


def line(t):
    print("\n" + t)
    print("-" * len(t))


def main():
    init_db()
    BRAND = "catchall_ireland"

    line("1) Admin assigns Yash 5 and Rahul 3")
    ya = engine.create_assignment("Yash", BRAND, 5)
    ra = engine.create_assignment("Rahul", BRAND, 3)
    print(f"  Yash : reserved {ya['reserved']}/{ya['requested']} (short {ya['short']})")
    print(f"  Rahul: reserved {ra['reserved']}/{ra['requested']} (short {ra['short']})")

    line("2) No photo given to both workers")
    overlap = set(ya["photo_ids"]) & set(ra["photo_ids"])
    print(f"  overlap: {len(overlap)}  (want 0)")

    line("3) Yash downloads all 5, then uploads 3 of them")
    engine.mark_downloaded(ya["assignment_id"], ya["photo_ids"])
    uploaded = ya["photo_ids"][:3]
    links = {p: f"https://drive.example/{i}" for i, p in enumerate(uploaded)}
    engine.mark_uploaded(ya["assignment_id"], uploaded, links)
    print(f"  marked {len(uploaded)} uploaded")

    line("4) Verify Yash's progress")
    prog = engine.assignment_progress(ya["assignment_id"])
    print(f"  assigned={prog['assigned']} uploaded={prog['uploaded']} "
          f"remaining={prog['remaining']} complete={prog['complete']}")
    print(f"  the exact remaining photos:")
    for pid in prog["remaining_ids"]:
        print("     -", pid.split('/')[-1])

    line("5) Admin overview (dashboard rows)")
    for r in engine.admin_overview():
        print(f"  {r['worker']:<8} {r['brand']:<18} "
              f"{r['uploaded']}/{r['assigned']} done, {r['remaining']} left")

    line("RESULT")
    ok = (overlap == set()
          and ya["reserved"] == 5 and ra["reserved"] == 3
          and prog["assigned"] == 5 and prog["uploaded"] == 3
          and prog["remaining"] == 2 and not prog["complete"])
    print("  ALL CHECKS PASSED" if ok else "  SOMETHING FAILED")


if __name__ == "__main__":
    main()
