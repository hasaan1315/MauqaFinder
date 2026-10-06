"""
sync_njp.py - keep the Supabase table raw.njp_jobs in sync with https://njp.gov.pk/jobs/live

One file: reads the job list (all pages), downloads the job pages, parses them and
writes them to Supabase. Same logic as sync_jobs.py. The database helpers
(get_db, db_read, db_upsert, db_retire) are shared and live in sync_jobs.py.
Needs the .env file (SUPABASE_URL, SUPABASE_SECRET_KEY) and the table raw.njp_jobs.

Every run compares the jobs listed on the site with the jobs stored in the table:

  listed on site, NOT in table             -> INSERT     (download page, parse, add)
  listed on site, inactive in table        -> REACTIVATE (re-download, update, is_active=true)
  listed on site, last date changed        -> UPDATE     (re-download, update)
  active in table, NOT listed anymore      -> DEACTIVATE (is_active=false) or DELETE
  everything else                          -> nothing, no download

A dropped connection is retried 2 times, 10 seconds apart. Downloaded pages are kept
in njp_pages/ and never downloaded twice (delete the folder to start fresh).

Run:   python sync_njp.py
       python sync_njp.py --reparse   (re-parse ALL listed jobs from the saved pages, no downloads;
                                       use it after the parser changed)
       python sync_njp.py --refresh   (re-download and re-parse ALL listed jobs)
       python sync_njp.py --force     (allow removing/updating many jobs at once)
"""
import os, re, sys, time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import sync_jobs as base                                  # database helpers

LIST_URL = "https://njp.gov.pk/jobs/live"
TABLE = "njp_jobs"                                        # raw.njp_jobs
PAGES_DIR = "njp_pages"
MAX_PAGES = 50
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DELAY = 3                                    # seconds between page downloads (be gentle)
RETRIES = 2                                  # retries per page (after the first try)
RETRY_WAIT = 10                              # seconds to wait before each retry

JOB_HREF = re.compile(r"/jobs/(\d+)/?$")                  # .../jobs/9692
NOT_A_TITLE = {"view details", "login to apply", "login to apply →", "apply"}
TOTAL_RE = re.compile(r"of\s+(\d+)\s+(?:jobs|results)", re.I)             # "of 41 jobs"
LIST_DATE = re.compile(r"(?:Available Till|Expired On)\s+([A-Za-z]{3}\s+\d{1,2}\s*,\s*\d{4})", re.I)

session = requests.Session()
session.headers.update(HEADERS)


# ------------------------------------------------------------------ download
def get_html(url):
    """Download one page. Retries (RETRIES times) when the site drops the connection. None = gave up."""
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  failed: {type(e).__name__}")
            if attempt < RETRIES:
                print(f"  retry {attempt + 1}/{RETRIES} in {RETRY_WAIT}s...")
                time.sleep(RETRY_WAIT)
    return None


# ------------------------------------------------------------------- parsing
def norm(s):
    return re.sub(r"\s+", " ", s).rstrip(":").strip().lower()


LABELS = {"grade", "job type", "vacancies", "job description", "eligibility criteria",
          "qualifications", "experience", "age limit", "application deadline",
          "quick overview", "about employer", "read more"}


def val(lines, label):
    """The line right after a label line (None if missing or if it is another label)."""
    for i, l in enumerate(lines):
        if norm(l) == label and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            return None if (not nxt or norm(nxt) in LABELS) else nxt
    return None


DATE = r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})"            # 09 Oct 2026


def to_iso(s):
    try:
        return datetime.strptime(re.sub(r"\s+", " ", s), "%d %b %Y").date().isoformat()
    except Exception:
        return None


def find_date(text, label):
    m = re.search(label + r":?\s*" + DATE, text)
    return to_iso(m.group(1)) if m else None


def get_description(lines):
    start = next((i for i, l in enumerate(lines) if norm(l) == "job description"), None)
    if start is None:
        return None
    out = []
    for l in lines[start + 1:]:
        if norm(l) == "eligibility criteria":
            break
        if norm(l) != "read more":
            out.append(l.strip())
    return "\n".join(out).strip() or None


def get_qualifications(lines):
    """Degree names listed under Eligibility Criteria > Qualifications."""
    elig = next((i for i, l in enumerate(lines) if norm(l) == "eligibility criteria"), None)
    if elig is None:
        return []
    start = next((i for i in range(elig, len(lines)) if norm(lines[i]) == "qualifications"), None)
    if start is None:
        return []
    out = []
    for l in lines[start + 1:]:
        if norm(l) in {"read more", "experience", "age limit", "application deadline"}:
            break
        if l.strip():
            out.append(l.strip())
    return out


def read_page(html):
    """-> (h1 title, text lines, all text in one line, 'Quick Overview' block)"""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else None
    for t in soup(["script", "style"]):
        t.decompose()
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    flat = " ".join(lines)
    qo = ""
    if "Quick Overview" in flat:                              # Posted / Deadline / Vacancies / ...
        qo = flat.split("Quick Overview", 1)[1].split("About Employer", 1)[0]
    return h1_text, lines, flat, qo


AGE_STOP = {"age limit", "minimum age limit", "application deadline", "read more", "quick overview",
            "about employer", "experience", "qualifications", "vacancies", "grade", "job type"}


def age_texts(lines, qo):
    """Raw age snippets from the page -> list of (text, what a bare number means: 'max' or 'min').
    Looks at: the Quick Overview box, every 'Age Limit' block and every 'Minimum Age Limit' block."""
    texts = []
    if "Age Limit" in qo:                                    # Quick Overview: "Age Limit: Max: 40"
        texts.append((qo.split("Age Limit", 1)[1][:60], "max"))
    for i, l in enumerate(lines):
        label = norm(l)
        if label in ("age limit", "minimum age limit"):
            block = []
            for x in lines[i + 1:i + 3]:                     # the 1-2 lines (pills) under the heading
                if norm(x) in AGE_STOP:
                    break
                block.append(x)
            if block:
                texts.append((" ".join(block), "min" if label == "minimum age limit" else "max"))
    return texts


def parse_age(text, bare="max"):
    """'Min: 18 Max: 35' | '18 - 35 years' | '18 to 35' | 'Max: 40' | '40 years' -> (min, max)
    A bare number ('40 years') is the maximum, or the minimum when bare="min"."""
    t = re.sub(r"\s+", " ", text or "")
    mn = re.search(r"\bmin(?:imum)?\.?\s*:?\s*(\d{1,2})", t, re.I)
    mx = re.search(r"\bmax(?:imum)?\.?\s*:?\s*(\d{1,2})", t, re.I)
    if mn or mx:
        return (int(mn.group(1)) if mn else None, int(mx.group(1)) if mx else None)
    r = re.search(r"(\d{1,2})\s*(?:-|–|—|to|and)\s*(\d{1,2})", t, re.I)     # a range
    if r:
        lo, hi = int(r.group(1)), int(r.group(2))
        return min(lo, hi), max(lo, hi)
    n = re.search(r"(\d{1,2})", t)                                          # one bare number
    if not n:
        return None, None
    return (int(n.group(1)), None) if bare == "min" else (None, int(n.group(1)))


def parse_job(job_id, list_title, html):
    h1_text, lines, flat, qo = read_page(html)
    title = h1_text or list_title

    employer = None
    if title in lines:                                       # line after the title = employer
        i = lines.index(title)
        if i + 1 < len(lines) and norm(lines[i + 1]) not in ("government of pakistan", "grade"):
            employer = lines[i + 1]

    vac = re.search(r"Vacancies:?\s*(\d+)", qo) or re.search(r"(\d+)\s*vacanc", flat, re.I)

    exp = None
    m = re.search(r"Experience:?\s*(?:(\d+)\s*\+?\s*years?|(no\s*experience))", qo, re.I)
    if m:
        exp = int(m.group(1)) if m.group(1) else 0

    age_min = age_max = None
    for text, bare in age_texts(lines, qo):
        lo, hi = parse_age(text, bare)
        age_min = age_min if age_min is not None else lo
        age_max = age_max if age_max is not None else hi

    deadline = find_date(qo, "Deadline")
    if deadline is None:                                     # fallback: "Application Deadline" box
        d = val(lines, "application deadline")
        m = re.search(DATE, d or "")
        deadline = to_iso(m.group(1)) if m else None

    return {
        "id": job_id,
        "title": title,
        "employer": employer,
        "job_type": val(lines, "job type"),
        "grade": val(lines, "grade"),
        "vacancies": int(vac.group(1)) if vac else None,
        "experience_years": exp,
        "age_min": age_min,
        "age_max": age_max,
        "qualifications": get_qualifications(lines),
        "description": get_description(lines),
        "job_posted": find_date(qo, "Posted"),
        "last_date_to_apply": deadline,
        "url": f"https://njp.gov.pk/jobs/{job_id}",
    }


# ------------------------------------------------------------------ site list
def card_date(a):
    """Last date shown on the job card of link `a`, as YYYY-MM-DD (None if not found)."""
    for p in a.parents:
        ids = {JOB_HREF.search(x["href"]).group(1) for x in p.find_all("a", href=JOB_HREF)}
        if len(ids) > 1:                                  # we left the card and reached the list
            break
        m = LIST_DATE.search(p.get_text(" ", strip=True))
        if m:
            text = re.sub(r",\s*", ", ", re.sub(r"\s+", " ", m.group(1)))
            try:
                return datetime.strptime(text, "%b %d, %Y").date().isoformat()
            except ValueError:
                return None
    return None


def fetch_list():
    """All jobs on all list pages. Stops the whole run if a page cannot be downloaded."""
    live, expected = {}, None
    for page in range(1, MAX_PAGES + 1):
        html = get_html(LIST_URL if page == 1 else f"{LIST_URL}?page={page}")
        if html is None:
            sys.exit(f"List page {page} failed. Stopping. Nothing was changed.")
        soup = BeautifulSoup(html, "lxml")
        if expected is None:
            m = TOTAL_RE.search(soup.get_text(" ", strip=True))
            expected = int(m.group(1)) if m else None

        new = 0
        for a in soup.find_all("a", href=JOB_HREF):
            job_id = JOB_HREF.search(a["href"]).group(1)
            if job_id not in live:
                live[job_id] = {"title": None, "url": f"https://njp.gov.pk/jobs/{job_id}",
                                "last_date": None}
                new += 1
            text = a.get_text(" ", strip=True)            # title link has the title as text,
            if text and text.lower() not in NOT_A_TITLE and not live[job_id]["title"]:
                live[job_id]["title"] = text              # "View Details" links are skipped
            if live[job_id]["last_date"] is None:
                live[job_id]["last_date"] = card_date(a)

        print(f"list page {page}: {new} new jobs")
        if new == 0:                                      # nothing new -> passed the last page
            break
        time.sleep(DELAY)

    complete = expected is None or expected == len(live)
    if not complete:
        print(f"WARNING: collected {len(live)} jobs but the site says {expected}. "
              "The list may have changed while reading. Nothing will be removed this run.")
    return live, complete


# ---------------------------------------------------------------------- sync
def main(refresh=False, force=False, reparse=False):
    live, complete = fetch_list()
    print("Listed on site:", len(live))
    if not live:
        sys.exit("No jobs found on the site. Stopping. Nothing was changed.")

    db = base.get_db()
    try:
        saved = base.db_read(db, "id,is_active,last_date_to_apply", tbl=TABLE)
    except Exception as e:
        sys.exit(f"Supabase is not reachable: {e}\n"
                 f"Check: table {base.DB_SCHEMA}.{TABLE} exists (run the CREATE TABLE SQL) and has GRANTs for "
                 "service_role.\nNothing was changed.")
    known = {r["id"]: {"is_active": r["is_active"], "last_date": r["last_date_to_apply"]}
             for r in saved}
    active = {i for i, k in known.items() if k["is_active"]}
    print(f"In Supabase: {len(active)} active ({len(known)} total)")

    # ---- decide what to do, based on the table ----
    new = [s for s in live if s not in known]                                  # INSERT
    back = [s for s in live if s in known and not known[s]["is_active"]]       # REACTIVATE
    changed = [s for s in live if s in active                                  # UPDATE
               and live[s]["last_date"] and known[s]["last_date"]
               and live[s]["last_date"] != known[s]["last_date"]]
    if active and len(changed) > max(3, len(active) // 2) and not force:
        print(f"WARNING: {len(changed)} of {len(active)} jobs look changed (date format difference?). "
              "Updates skipped. Use --refresh to re-scrape everything.")
        changed = []
    gone = [s for s in active if s not in live] if complete else []            # DEACTIVATE

    # safety: a broken page must not wipe the whole table
    if active and len(gone) > max(3, len(active) // 2) and not force:
        sys.exit(f"Would remove {len(gone)} of {len(active)} jobs. Looks wrong. "
                 "Nothing changed. If this is real, run: python sync_njp.py --force")

    # ---- download + parse what is needed ----
    os.makedirs(PAGES_DIR, exist_ok=True)
    todo = list(live) if (refresh or reparse) else new + back + changed
    redownload = set(live) if refresh else set(back) | set(changed)
    rows, c_new, c_back, c_upd, failed = [], 0, 0, 0, 0
    for job_id in todo:
        path = os.path.join(PAGES_DIR, job_id + ".html")
        try:
            if job_id in redownload or not os.path.exists(path):
                html = get_html(live[job_id]["url"])
                if html is None:
                    raise RuntimeError("download failed")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)
                time.sleep(DELAY)
            else:                                         # page already saved earlier
                with open(path, encoding="utf-8") as f:
                    html = f.read()
            rows.append(parse_job(job_id, live[job_id]["title"], html))
        except Exception as e:
            failed += 1
            print("FAIL", job_id, e)                      # not stored -> retried next run
            continue
        if job_id in new:
            c_new += 1
            print("NEW ", job_id)
        elif job_id in back:
            c_back += 1
            print("BACK", job_id)
        else:
            c_upd += 1
            print("UPD ", job_id)

    # ---- write to Supabase ----
    try:
        base.db_upsert(db, rows, tbl=TABLE)
        base.db_retire(db, gone, tbl=TABLE)
    except Exception as e:
        sys.exit(f"Supabase write FAILED: {e}\n"
                 "Nothing is marked as done, so the next run retries it.")
    for job_id in gone:
        print("DEL " if base.DB_HARD_DELETE else "OFF ", job_id)
        p = os.path.join(PAGES_DIR, job_id + ".html")
        if os.path.exists(p):
            os.remove(p)

    active_now = len((active - set(gone)) | {r["id"] for r in rows})
    print(f"\nNew: {c_new} | Reactivated: {c_back} | Updated: {c_upd} | "
          f"Removed: {len(gone)} | Failed: {failed} | Active now: {active_now}")
    if not (c_new or c_back or c_upd or gone or failed):
        print("No changes.")


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv, force="--force" in sys.argv, reparse="--reparse" in sys.argv)