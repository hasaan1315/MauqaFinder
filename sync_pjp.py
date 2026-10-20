"""
sync_jobs.py - Master Sync for Punjab Jobs Portal with DataTable JS Injection
"""
import re, os, sys, json, time
from datetime import datetime, date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = "https://jobs.punjab.gov.pk/new_recruit/jobs"
DELAY = 1.5                      
LINKS_FILE = "job_links.json"
PAGES_DIR = "pages"
DROP_PAST_DEADLINE = False       
DETECT_CHANGES = True            
DB_SCHEMA = os.getenv("SUPABASE_SCHEMA", "raw")             
DB_TABLE = os.getenv("SUPABASE_TABLE", "punjab_jobs_portal")  
DB_HARD_DELETE = False           

# --- SCRAPERAPI REST ENDPOINT SETUP ---
api_key = os.getenv("SCRAPERAPI_KEY")
if not api_key:
    print("WARNING: SCRAPERAPI_KEY not found. Operations will likely fail.")

def fetch_via_scraperapi(target_url: str, retries: int = 3, exit_on_fail: bool = False, get_all_rows: bool = False) -> str:
    """Routes request through ScraperAPI with JS injection to expand DataTables to 200 rows."""
    proxy_url = "https://api.scraperapi.com/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }
    payload = {
        'api_key': api_key,
        'url': target_url,
        'premium': 'true',        
        'country_code': 'pk',     
        'render': 'true',         
        'keep_headers': 'true'
    }
    
    # Inject JavaScript to command DataTables to load all entries onto a single page
    if get_all_rows:
        payload['js_instructions'] = json.dumps([
            {"wait": 2000},
            {"evaluate": "if (typeof $ !== 'undefined' && $.fn.dataTable) { $('.dataTable').DataTable().page.len(200).draw(); } else { var sel = document.querySelector('select[name$=\\'_length\\']'); if(sel) { sel.value = '200'; sel.dispatchEvent(new Event('change')); } }"},
            {"wait": 4000}
        ])
        
    for attempt in range(retries):
        try:
            r = requests.get(proxy_url, headers=headers, params=payload, timeout=90)
            if r.status_code == 200:
                return r.text
            print(f"  Fetch attempt {attempt + 1}/{retries} failed: Status {r.status_code}. Retrying...")
            time.sleep(3)
        except requests.exceptions.RequestException as e:
            print(f"  Fetch attempt {attempt + 1}/{retries} network error: {e}. Retrying...")
            time.sleep(3)
            
    error_msg = f"Failed to fetch {target_url} after {retries} attempts."
    if exit_on_fail:
        print(f"⚠️ {error_msg} Exiting gracefully without failing workflow.")
        sys.exit(0) 
    else:
        raise Exception(error_msg)


# ------------------------------------------------------------------ fetching
LIST_DATE_RE = re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b")

def list_date(tr):
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
    """Fetch PJP listing and diagnose missing job links."""
    print("Fetching PJP listing through ScraperAPI...")

    html = fetch_via_scraperapi(
        BASE,
        exit_on_fail=False,
        get_all_rows=True,
    )

    soup = BeautifulSoup(html, "lxml")
    live = {}

    print(f"HTML length: {len(html):,} characters")
    print(f"DataTables elements: {len(soup.select('.dataTable'))}")
    print(f"Table rows: {len(soup.select('table tr'))}")
    print(
        "Pagination controls:",
        len(soup.select(
            ".paginate_button, .pagination, "
            "[aria-label*='page']"
        ))
    )

    links = soup.find_all(
        "a",
        href=re.compile(r"job_detail", re.I)
    )
    print(f"Job-detail links found: {len(links)}")

    for a in links:
        href = a.get("href")
        if not href:
            continue

        url = urljoin(BASE, href)
        slug = url.rstrip("/").split("/")[-1]

        if not slug or slug in live:
            continue

        live[slug] = {
            "title": a.get_text(" ", strip=True),
            "url": url,
            "last_date": list_date(a.find_parent("tr")),
        }

    print(f"Unique jobs extracted: {len(live)}")
    return live



def fetch_page(url):
    return fetch_via_scraperapi(url, exit_on_fail=False, get_all_rows=False)


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
        return None if d.year <= 1970 else d.isoformat()   
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
    edu = []
    for l in lines:
        m = EDU_RE.match(l.strip())
        if m:
            edu.append(int(m.group(2)))

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
    while True:                                      
        q = table(db, tbl).select(columns)
        if only_active:
            q = q.eq("is_active", True)
        rows = q.order("id").range(start, start + 999).execute().data
        out += rows
        if len(rows) < 1000:
            return out
        start += 1000

def db_upsert(db, rows, tbl=None):
    rows = [{**json.loads(json.dumps(r)), "is_active": True} for r in rows]   
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
        sys.exit(f"Supabase is not reachable: {e}\nNothing was changed.")
    known = {r["id"]: {"is_active": r["is_active"], "last_date": r["last_date_to_apply"]}
             for r in saved}
    active = {i for i, k in known.items() if k["is_active"]}
    print(f"In Supabase: {len(active)} active ({len(known)} total)")

    # ---- decide what to do, based on the table ----
    new = [s for s in live if s not in known]                                  
    back = [s for s in live if s in known and not known[s]["is_active"]]       
    changed = []                                                               
    if DETECT_CHANGES:
        changed = [s for s in live if s in active
                   and live[s]["last_date"] and known[s]["last_date"]
                   and live[s]["last_date"] != known[s]["last_date"]]
        if active and len(changed) > max(3, len(active) // 2) and not force:
            print(f"WARNING: {len(changed)} of {len(active)} jobs look changed "
                  "(date format difference?). Updates skipped. "
                  "Use --refresh to re-scrape everything.")
            changed = []
    gone = [s for s in active if s not in live]                                

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
            else:                              
                with open(path, encoding="utf-8") as f:
                    html = f.read()
            rows.append(parse_job(slug, live[slug]["title"], html))
        except Exception as e:
            failed += 1
            print("FAIL", slug, e)             
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
        if rows:
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