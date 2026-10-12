"""
sync_jobs.py - keep the Supabase table raw.punjab_jobs_portal in sync with
https://jobs.punjab.gov.pk/new_recruit/jobs

Needs a .env file with SUPABASE_URL and SUPABASE_SECRET_KEY.

Every run compares the jobs listed on the site with the jobs stored in the table:

  listed on site, NOT in table             -> INSERT     (download page, parse, add)
  listed on site, inactive in table        -> REACTIVATE (re-download, update, is_active=true)
  listed on site, last date changed        -> UPDATE     (re-download, update)
  active in table, NOT listed anymore      -> DEACTIVATE (is_active=false) or DELETE
  everything else                          -> nothing, no download

Run:   python sync_jobs.py
       python sync_jobs.py --refresh   (re-download and re-parse ALL listed jobs)
       python sync_jobs.py --force     (allow removing/updating many jobs at once)
"""
import re, os, sys, json, time
from datetime import datetime, date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:                              # read SUPABASE_* from the .env file if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = "https://jobs.punjab.gov.pk/new_recruit/jobs"
HEADERS = {"User-Agent": "Mozilla/5.0"}
DELAY = 1.5                      # seconds between detail-page requests
LINKS_FILE = "job_links.json"
PAGES_DIR = "pages"
DROP_PAST_DEADLINE = False       # True = also remove jobs whose last date has passed
DETECT_CHANGES = True            # re-scrape a job when its last date on the list page changed
DB_SCHEMA = os.getenv("SUPABASE_SCHEMA", "raw")             # Supabase schema (RAW layer)
DB_TABLE = os.getenv("SUPABASE_TABLE", "punjab_jobs_portal")  # table for this source
DB_HARD_DELETE = False           # False = expired jobs get is_active=false (soft delete)
                                 # True  = expired jobs are deleted from the table


# ------------------------------------------------------------------ fetching
LIST_DATE_RE = re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b")   # 18-Oct-2026


def list_date(tr):
    """Last apply date shown in the list row, as YYYY-MM-DD (or None)."""
    if tr is None:
        return None
    m = LIST_DATE_RE.search(tr.get_text(" ", strip=True))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def fetch_list():
    r = requests.get(BASE, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    live = {}
    for a in soup.find_all("a", href=re.compile(r"job_detail")):
        url = urljoin(BASE, a["href"])
        slug = url.rstrip("/").split("/")[-1]
        if slug not in live:
            live[slug] = {"title": a.get_text(strip=True), "url": url,
                          "last_date": list_date(a.find_parent("tr"))}
    return live


def fetch_page(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


# ------------------------------------------------------------------- parsing
def get_lines(html):
    s = BeautifulSoup(html, "lxml")
    text = s.get_text("\n", strip=True)
    a, b = text.find("Job Details"), text.find("Reset Password")
    if a < 0:
        a = 0
    return text[a:b if b > a else None].split("\n")


def norm(s):
    return re.sub(r"\s+", " ", s).rstrip(":").strip().lower()


# if the "value" line is really another label, the field is empty
LABELS = {"division", "district", "industry", "project", "total positions",
          "employment status", "role", "job posted", "level", "last date to apply",
          "gender", "age", "preferred candidates", "years of experience",
          "degree level", "years of education", "degree area", "email address",
          "send email", "important note", "back to listings", "apply for this job",
          "job description", "job details"}


def val(lines, *labels):
    for lab in labels:
        for i, l in enumerate(lines):
            if norm(l) == lab.lower():
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if not nxt or norm(nxt) in LABELS:
                    return None
                return nxt
    return None


def to_iso(s):
    try:
        d = datetime.strptime(s, "%d-%m-%Y").date()
        return None if d.year <= 1970 else d.isoformat()   # 1970 = empty placeholder
    except Exception:
        return None


def to_int(s):
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else None


END = {"degree level", "years of education", "degree area", "important note",
       "apply for this job", "view advertisement", "sitemap", "preferred candidates"}


def get_description(lines):
    for i, l in enumerate(lines):
        if norm(l) in ("job description", "description"):
            out = []
            for x in lines[i + 1:]:
                if norm(x) in END:
                    break
                out.append(x)
            t = " ".join(out)
            t = re.sub(r"\s*[·•]\s*", "\n- ", t)
            t = re.sub(r"[ \t\u00a0]+", " ", t)
            t = re.sub(r"\n\s+", "\n", t).strip()
            return t or None
    return None


EDU_RE = re.compile(r"^(.*?)\s*\((\d+)\s*years?\)\s*:?\s*$", re.I)
EXP_END = {"email address", "send email", "×", "degree level", "years of education",
           "degree area", "important note", "apply for this job",
           "job description", "description"}


def parse_edu_exp(lines):
    # education: every "Degree (N years)" line -> keep the minimum
    edu = []
    for l in lines:
        m = EDU_RE.match(l.strip())
        if m:
            edu.append(int(m.group(2)))

    # experience: ONLY inside the "Years of Experience" block
    exp = []
    start = next((i for i, l in enumerate(lines) if norm(l) == "years of experience"), None)
    if start is not None:
        block = []
        for l in lines[start + 1:]:
            if norm(l) in EXP_END:
                break
            block.append(l)
        for i, l in enumerate(block):
            m = EDU_RE.match(l.strip())
            if not m:
                continue
            nxt = block[i + 1].strip() if i + 1 < len(block) else ""
            n = re.match(r"^\d+", nxt)
            if n:
                item = {int(m.group(2)): int(n.group(0))}
                if item not in exp:
                    exp.append(item)
    return (min(edu) if edu else None), exp


def get_degree_area(lines):
    for i, l in enumerate(lines):
        if norm(l) == "degree area":
            out = []
            for x in lines[i + 1:]:
                n = norm(x)
                if n in END or x.strip().isdigit():
                    break
                if x.strip() not in out:
                    out.append(x.strip())
            return out
    return []


def get_age(lines):
    for i, l in enumerate(lines):
        if re.match(r"^age\b", norm(l)):
            chunk = l + " " + (lines[i + 1] if i + 1 < len(lines) else "")
            m = re.search(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})", chunk)
            if m:
                return int(m.group(1)), int(m.group(2))
            m = re.search(r"\b(\d{1,2})\b", chunk)
            if m:
                return None, int(m.group(1))
    return None, None


def salary_text(lines):
    v = val(lines, "Monthly Salary", "Salary", "Pay Scale", "Salary Range")
    if v:
        return v
    m = re.search(r"(?:Rs\.?|PKR)\s*[\d,]+(?:\s*(?:-|–|to)\s*(?:Rs\.?|PKR)?\s*[\d,]+)?",
                  " ".join(lines), re.I)
    return m.group(0) if m else None


def parse_salary(text):
    if not text:
        return None, None
    nums = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", text)]
    nums = [n for n in nums if n >= 1000][:2]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return min(nums), max(nums)


def parse_job(slug, title, html):
    L = get_lines(html)
    edu_min, exp = parse_edu_exp(L)
    amin, amax = get_age(L)
    smin, smax = parse_salary(salary_text(L))
    return {
        "id": slug,
        "title": title,
        "description": get_description(L),
        "education_level_years": edu_min,
        "degree_area": get_degree_area(L),
        "district": val(L, "District"),
        "division": val(L, "Division"),
        "industry": val(L, "Industry"),
        "project": val(L, "Project"),
        "total_position": to_int(val(L, "Total Positions")),
        "employment_status": val(L, "Employment Status"),
        "role": val(L, "Role"),
        "job_posted": to_iso(val(L, "Job Posted")),
        "last_date_to_apply": to_iso(val(L, "Last Date to Apply")),
        "level": val(L, "Level"),
        "years_of_experience": exp,
        "age_min": amin,
        "age_max": amax,
        "gender": val(L, "Gender"),
        "salary_min": smin,
        "salary_max": smax,
    }


# ------------------------------------------------------------------ database
def get_db():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY in the .env file.")
    from supabase import create_client
    return create_client(url, key)


def table(db, tbl=None):
    return db.schema(DB_SCHEMA).table(tbl or DB_TABLE)


def db_read(db, columns, only_active=False, tbl=None):
    out, start = [], 0
    while True:                              # PostgREST returns max 1000 rows per request
        q = table(db, tbl).select(columns)
        if only_active:
            q = q.eq("is_active", True)
        rows = q.order("id").range(start, start + 999).execute().data
        out += rows
        if len(rows) < 1000:
            return out
        start += 1000


def db_upsert(db, rows, tbl=None):
    rows = [{**json.loads(json.dumps(r)), "is_active": True} for r in rows]   # int keys -> str
    for i in range(0, len(rows), 100):
        table(db, tbl).upsert(rows[i:i + 100], on_conflict="id").execute()


def db_retire(db, ids, tbl=None):
    if not ids:
        return
    if DB_HARD_DELETE:
        table(db, tbl).delete().in_("id", ids).execute()
    else:
        table(db, tbl).update({"is_active": False}).in_("id", ids).execute()


# ---------------------------------------------------------------------- sync
def main(refresh=False, force=False):
    live = fetch_list()
    print("Listed on site:", len(live))
    if not live:
        sys.exit("No jobs found on the site. Stopping. Nothing was changed.")

    db = get_db()
    try:
        saved = db_read(db, "id,is_active,last_date_to_apply")
    except Exception as e:
        hint = ""
        if "PGRST205" in str(e) or "PGRST106" in str(e) or "schema" in str(e).lower():
            hint = (f"\nCheck: table {DB_SCHEMA}.{DB_TABLE} exists, schema '{DB_SCHEMA}' is in "
                    "Supabase Settings > API > Exposed schemas, and service_role has GRANTs on it.")
        sys.exit(f"Supabase is not reachable: {e}{hint}\nNothing was changed.")
    known = {r["id"]: {"is_active": r["is_active"], "last_date": r["last_date_to_apply"]}
             for r in saved}
    active = {i for i, k in known.items() if k["is_active"]}
    print(f"In Supabase: {len(active)} active ({len(known)} total)")

    # ---- decide what to do, based on the table ----
    new = [s for s in live if s not in known]                                  # INSERT
    back = [s for s in live if s in known and not known[s]["is_active"]]       # REACTIVATE
    changed = []                                                               # UPDATE
    if DETECT_CHANGES:
        changed = [s for s in live if s in active
                   and live[s]["last_date"] and known[s]["last_date"]
                   and live[s]["last_date"] != known[s]["last_date"]]
        if active and len(changed) > max(3, len(active) // 2) and not force:
            print(f"WARNING: {len(changed)} of {len(active)} jobs look changed "
                  "(date format difference?). Updates skipped. "
                  "Use --refresh to re-scrape everything.")
            changed = []
    gone = [s for s in active if s not in live]                                # DEACTIVATE

    if DROP_PAST_DEADLINE:
        today = date.today().isoformat()
        gone += [s for s in active if s in live and known[s]["last_date"]
                 and known[s]["last_date"] < today]

    # safety: a broken page must not wipe the whole table
    if active and len(gone) > max(3, len(active) // 2) and not force:
        sys.exit(f"Would remove {len(gone)} of {len(active)} jobs. Looks wrong. "
                 "Nothing changed. If this is real, run: python sync_jobs.py --force")

    # ---- download + parse what is needed ----
    os.makedirs(PAGES_DIR, exist_ok=True)
    todo = list(live) if refresh else new + back + changed
    redownload = set(live) if refresh else set(back) | set(changed)
    rows, c_new, c_back, c_upd, failed = [], 0, 0, 0, 0
    for slug in todo:
        path = os.path.join(PAGES_DIR, slug + ".html")
        try:
            if slug in redownload or not os.path.exists(path):
                html = fetch_page(live[slug]["url"])
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)
                time.sleep(DELAY)
            else:                              # page already saved earlier
                with open(path, encoding="utf-8") as f:
                    html = f.read()
            rows.append(parse_job(slug, live[slug]["title"], html))
        except Exception as e:
            failed += 1
            print("FAIL", slug, e)             # not stored -> retried next run
            continue
        if slug in new:
            c_new += 1
            print("NEW ", slug)
        elif slug in back:
            c_back += 1
            print("BACK", slug)
        else:
            c_upd += 1
            print("UPD ", slug)

    # ---- write to Supabase ----
    try:
        db_upsert(db, rows)
        db_retire(db, gone)
    except Exception as e:
        sys.exit(f"Supabase write FAILED: {e}\n"
                 "Nothing is marked as done, so the next run retries it.")
    for slug in gone:
        print("DEL " if DB_HARD_DELETE else "OFF ", slug)
        p = os.path.join(PAGES_DIR, slug + ".html")
        if os.path.exists(p):
            os.remove(p)

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump([{"title": v["title"], "url": v["url"]} for v in live.values()],
                  f, ensure_ascii=False, indent=2)

    active_now = len((active - set(gone)) | {r["id"] for r in rows})
    print(f"\nNew: {c_new} | Reactivated: {c_back} | Updated: {c_upd} | "
          f"Removed: {len(gone)} | Failed: {failed} | Active now: {active_now}")
    if not (c_new or c_back or c_upd or gone or failed):
        print("No changes.")


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv, force="--force" in sys.argv)