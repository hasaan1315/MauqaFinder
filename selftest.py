"""
selftest.py - proves sync_jobs.py works on YOUR Supabase table.

Run:  python selftest.py

TEST 1  nothing changed   -> the active jobs must stay the same
TEST 2  one real job is switched off in the table + one FAKE job is inserted
        -> sync must bring the real job back and switch the fake one off
CHECK   active jobs in the table must equal the jobs listed in job_links.json
CLEANUP the fake job is deleted from the table at the end

The site is hit 2 times (list page) plus 1 detail page (the real job that was switched off).
"""
import json
import sync_jobs as s

FAKE_ID = "fake-expired-job"


def active_ids(db):
    return {r["id"] for r in s.db_read(db, "id", only_active=True)}


def run():
    db = s.get_db()
    results = []
    try:
        print("\n===== TEST 1: nothing changed =====")
        before = active_ids(db)
        s.main()
        results.append(("Test 1: same active jobs after a no-change run", active_ids(db) == before))

        print("\n===== TEST 2: switch one job off + insert one fake =====")
        victim = sorted(active_ids(db))[0]
        s.db_retire(db, [victim])
        s.db_upsert(db, [{"id": FAKE_ID, "title": "FAKE EXPIRED JOB"}])
        print("switched off:", victim, "| inserted fake:", FAKE_ID)
        s.main()
        after = active_ids(db)
        results.append(("Test 2a: switched-off job came back", victim in after))
        results.append(("Test 2b: fake job was switched off", FAKE_ID not in after))

        print("\n===== CHECK: counts =====")
        with open(s.LINKS_FILE, encoding="utf-8") as f:
            n_links = len(json.load(f))
        print(f"active in table={len(after)} | job_links.json={n_links}")
        results.append(("Counts match", len(after) == n_links))
    finally:
        s.table(db).delete().eq("id", FAKE_ID).execute()      # remove the fake row for good

    print("\n===== RESULT =====")
    for name, ok in results:
        print("PASS" if ok else "FAIL", "-", name)


if __name__ == "__main__":
    run()