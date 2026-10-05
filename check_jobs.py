"""
check_jobs.py - read-only report on the ACTIVE jobs in Supabase (changes nothing)

Run:  python check_jobs.py
Writes (to look at, or to send for parser fixes):
  age_debug.txt     raw age lines of jobs where age is null
  salary_debug.txt  raw salary text and what was parsed from it
  odd_jobs.txt      raw text of the first 5 jobs missing description or education
The raw text comes from the saved pages/ folder.
"""
import os, re, sys
import sync_jobs as s

db = s.get_db()
jobs = s.db_read(db, "*", only_active=True)
if not jobs:
    sys.exit("No active jobs in the table.")

print(len(jobs), "active jobs |", len(jobs[0]), "fields\n")
for k in jobs[0]:
    n = sum(1 for j in jobs if j[k] not in (None, [], ""))
    print(f"{k:24} {n}/{len(jobs)}")

print("\nJobs with 2+ experience entries:")
for j in jobs:
    if len(j["years_of_experience"] or []) > 1:
        print(" ", j["id"], j["years_of_experience"])


def page_lines(job):
    path = os.path.join(s.PAGES_DIR, job["id"] + ".html")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return s.get_lines(f.read())


age_missing = odd = 0
with open("age_debug.txt", "w", encoding="utf-8") as fa, \
     open("salary_debug.txt", "w", encoding="utf-8") as fs, \
     open("odd_jobs.txt", "w", encoding="utf-8") as fo:
    for j in jobs:
        L = page_lines(j)
        if L is None:
            continue

        if j["age_min"] is None and j["age_max"] is None:
            age_missing += 1
            fa.write(f"===== {j['id']} =====\n")
            hits = [i for i, l in enumerate(L) if re.search(r"\bage\b", l, re.I)]
            if not hits:
                fa.write("(no age line on this page)\n")
            for i in hits:
                fa.write("\n".join(L[max(0, i - 1): i + 3]) + "\n---\n")
            fa.write("\n")

        raw = s.salary_text(L)
        if raw:
            fs.write(f"{j['id']} | {raw!r} => {s.parse_salary(raw)}\n")

        if (not j["description"] or j["education_level_years"] is None) and odd < 5:
            odd += 1
            fo.write(f"===== {j['id']} =====\n" + "\n".join(L) + "\n\n")

print(f"\nAge null: {age_missing} -> age_debug.txt")
print("Salary raw text -> salary_debug.txt")
print(f"Odd jobs written: {odd} -> odd_jobs.txt")