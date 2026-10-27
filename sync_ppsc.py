"""
sync_ppsc.py - keep the Supabase table raw.ppsc_jobs in sync with PPSC active jobs.

Now uses DELTA-SYNC logic: It only performs the heavy ASP.NET POST requests
and HTML parsing for jobs that are NEW, REACTIVATED, or CHANGED.
"""
import os
import re
import sys
import time
import json
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import sync_pjp as base  # shared Supabase database helpers

# Load environment variables (crucial for local testing)
load_dotenv()

START_URL = "https://www.ppsc.gop.pk/Jobs.aspx"
TABLE = "ppsc_jobs"
PAGES_DIR = "ppsc_pages"
DETECT_CHANGES = True

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
DELAY = 3
RETRIES = 2

# --- SCRAPERAPI REST ENDPOINT SETUP ---
api_key = os.getenv("SCRAPERAPI_KEY")
if not api_key:
    print("WARNING: SCRAPERAPI_KEY not found. Operations will likely fail.")

def get_html(url, method="GET", data=None, retries=RETRIES, exit_on_fail=False):
    """Download page via ScraperAPI REST endpoint with retry logic and ASP.NET POST support."""
    if not api_key:
        # Fallback local request if no API key present
        try:
            if method == "POST":
                r = requests.post(url, data=data, headers=HEADERS, timeout=30)
            else:
                r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r
        except Exception as e:
            print(f"  Direct fetch failed: {e}")
            return None

    proxy_url = "https://api.scraperapi.com/"
    payload = {
        'api_key': api_key,
        'url': url,
        'premium': 'true',        
        'country_code': 'pk',     
        'render': 'true',         
        'keep_headers': 'true'
    }

    for attempt in range(retries + 1):
        try:
            if method == "POST":
                r = requests.post(proxy_url, params=payload, headers=HEADERS, data=data, timeout=90)
            else:
                r = requests.get(proxy_url, params=payload, headers=HEADERS, timeout=90)
            
            if r.status_code == 200:
                return r
            print(f"  Fetch attempt {attempt + 1}/{retries + 1} failed: Status {r.status_code}. Retrying...")
            time.sleep(3)
        except requests.RequestException as e:
            print(f"  failed: {type(e).__name__} ({e})")
            if attempt < retries:
                print(f"  retry {attempt + 1}/{retries}...")
                time.sleep(5)

    if exit_on_fail:
        print(f"⚠️ Failed to reach {url} after retries. Exiting gracefully without failing workflow.")
        sys.exit(0)
    return None


def parse_experience(text):
    if not text:
        return 0
    
    words_to_num = {
        'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 
        'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10',
        'eleven': '11', 'twelve': '12'
    }
    
    clean_text = text.lower()
    for word, num in words_to_num.items():
        clean_text = re.sub(rf'\b{word}\b', num, clean_text)
        
    # Super-flexible regex: Looks for "digits" + "years" + up to 10 intermediate words + "experience"
    match = re.search(r"(\d+)\s*(?:years?|yrs?)['\s]*s?(?:\s+[\w\'-]+){0,10}\s+experience", clean_text)
    
    if match:
        return int(match.group(1))
        
    # Fallback search if the word 'years' comes way later
    fallback_match = re.search(r"(\d+)\s*(?:[\w\s,\'-]+){0,15}years?['\s]*s?\s+experience", clean_text)
    return int(fallback_match.group(1)) if fallback_match else 0


def fetch_list():
    """Scrapes the main list lightly to get metadata without doing deep POSTs yet."""
    res = get_html(START_URL, exit_on_fail=True)
    if not res:
        print("Failed to reach PPSC landing page.")
        sys.exit(0)
    
    session_url = res.url 
    soup = BeautifulSoup(res.text, 'lxml')
    live_list = {}

    rows = soup.select("table tr")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 8:
            continue
            
        case_no = cols[2].get_text(strip=True)
        title = cols[3].get_text(strip=True)
        fee_text = cols[4].get_text(strip=True)
        department = cols[5].get_text(strip=True)
        closing_text = cols[7].get_text(strip=True)
        
        if not case_no or not title:
            continue
            
        closing_date = None
        try:
            closing_date = datetime.strptime(closing_text, "%d-%m-%Y").date().isoformat()
        except ValueError:
            pass

        fee = int(re.search(r'\d+', fee_text).group()) if re.search(r'\d+', fee_text) else 600

        a_tag = cols[3].find("a")
        target_id = None
        if a_tag and a_tag.has_attr("href"):
            match = re.search(r"__doPostBack\('([^']+)'", a_tag["href"])
            if match:
                target_id = match.group(1)

        if target_id:
            record_id = re.sub(r'[^a-zA-Z0-9]', '_', case_no).lower()
            live_list[record_id] = {
                "case_no": case_no,
                "title": title,
                "department": department,
                "fee": fee,
                "last_date": closing_date,
                "target": target_id,
                "session_url": session_url
            }

    return live_list


def fetch_detail_html(job_info):
    """Executes the heavy ASP.NET POST to grab deep details."""
    fresh_res = get_html(job_info["session_url"])
    if not fresh_res: return None
    
    fresh_soup = BeautifulSoup(fresh_res.text, 'lxml')
    viewstate = fresh_soup.select_one("#__VIEWSTATE")
    eventval = fresh_soup.select_one("#__EVENTVALIDATION")
    
    if not viewstate:
        return None

    form_data = {
        "__EVENTTARGET": job_info["target"],
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate.get("value", ""),
        "__EVENTVALIDATION": eventval.get("value", "") if eventval else ""
    }

    detail_res = get_html(job_info["session_url"], method="POST", data=form_data)
    return detail_res.text if detail_res else None


def extract_job_details(html, slug, job_info):
    """Parses the deep detail page for rich fields."""
    soup = BeautifulSoup(html, "lxml")
    
    for t in soup(["script", "style"]):
        t.decompose()
        
    text = soup.get_text(" ", strip=True)
    
    vac_match = re.search(r'Total Number of Posts:\s*(\d+)', text, re.I)
    vacancies = int(vac_match.group(1)) if vac_match else 1

    grade_match = re.search(r'(BS-\d+|BPS-\d+)', text, re.I)
    grade = grade_match.group(1).upper() if grade_match else None

    posting_match = re.search(r'Place Of Posting:\s*([A-Za-z\s,]+?)(?:Additional Requirement|Research|Preferences|Fee|$)', text, re.I)
    place_of_posting = posting_match.group(1).strip() if posting_match else "Punjab"

    elig_match = re.search(r'Eligibililty:\s*([^\n]+?)(?:Place Of Posting|$)', text, re.I)
    eligibility = elig_match.group(1).strip() if elig_match else None

    qual_match = re.search(r'Qualification:\s*(.*?)(?:Total Number of Posts|Experience:|Pay Scale:|$)', text, re.I | re.DOTALL)
    if qual_match:
        qualification_text = qual_match.group(1).strip()
        qualification_text = re.sub(r'^[\)\(\]\}\s*]+', '', qualification_text)
        qualification_text = re.sub(r'\bGraduation\b', "Bachelor's Degree", qualification_text, flags=re.I)
    else:
        qualification_text = None

    experience_years = parse_experience(qualification_text)

    age_min = age_max_male = age_max_female = None
    age_match = re.search(r'Age:\s*(.*?)(?:Eligibililty:|Eligibility:|Place Of Posting:|Fee:|Gender:|$)', text, re.I)
    
    if age_match:
        age_text = age_match.group(1)
        
        male_match = re.search(r'Male:\s*(\d+)\s*to\s*(\d+)(?:\s*\+\s*\d+\s*=\s*(\d+))?', age_text, re.I)
        if male_match:
            age_min = int(male_match.group(1))
            age_max_male = int(male_match.group(3)) if male_match.group(3) else int(male_match.group(2))
            
        female_match = re.search(r'Female:\s*\d+\s*to\s*(\d+)(?:\s*\+\s*\d+\s*=\s*(\d+))?', age_text, re.I)
        if female_match:
            age_max_female = int(female_match.group(2)) if female_match.group(2) else int(female_match.group(1))
            
        if not age_min and not age_max_male:
            gen_match = re.search(r'(\d+)\s*to\s*(\d+)', age_text, re.I)
            if gen_match:
                age_min = int(gen_match.group(1))
                age_max_male = int(gen_match.group(2))
                age_max_female = int(gen_match.group(2))

    return {
        "id": slug,
        "title": job_info["title"],
        "department": job_info["department"],
        "grade": grade,
        "vacancies": vacancies,
        "description": f"{job_info['title']} at {job_info['department']}. Case No: {job_info['case_no']}",
        "qualification_text": qualification_text,
        "experience_years": experience_years,
        "eligibility": eligibility,
        "place_of_posting": place_of_posting,
        "fee": job_info["fee"],
        "closing_date": job_info["last_date"],
        "url": job_info["session_url"],
        "age_min": age_min,
        "age_max_male": age_max_male,
        "age_max_female": age_max_female,
        "is_active": True
    }


def main(refresh=False, force=False):
    live = fetch_list()
    print("Listed on PPSC site:", len(live))
    if not live:
        print("No jobs found on the site. Stopping. Nothing was changed.")
        sys.exit(0)

    db = base.get_db()
    try:
        # Note: Your database uses "closing_date" for PPSC instead of "last_date_to_apply"
        saved = base.db_read(db, "id,is_active,closing_date", tbl=TABLE)
    except Exception as e:
        print(f"Supabase connection error for {TABLE}: {e}")
        sys.exit(1)

    known = {r["id"]: {"is_active": r["is_active"], "last_date": r["closing_date"]}
             for r in saved}
    active = {i for i, k in known.items() if k["is_active"]}
    print(f"In Supabase: {len(active)} active ({len(known)} total)")

    # ---- Delta Sync Logic ----
    new = [s for s in live if s not in known]
    back = [s for s in live if s in known and not known[s]["is_active"]]
    changed = []
    
    if DETECT_CHANGES:
        changed = [s for s in live if s in active
                   and live[s]["last_date"] and known[s]["last_date"]
                   and live[s]["last_date"] != known[s]["last_date"]]

    gone = [s for s in active if s not in live]

    if active and len(gone) > max(3, len(active) // 2) and not force:
        print(f"Would remove {len(gone)} PPSC jobs. Run with --force if intentional.")
        sys.exit(1)

    # ---- Fetching & Parsing Only What's Needed ----
    os.makedirs(PAGES_DIR, exist_ok=True)
    todo = list(live) if refresh else new + back + changed
    redownload = set(live) if refresh else set(back) | set(changed)
    
    rows, c_new, c_back, c_upd, failed = [], 0, 0, 0, 0

    print(f"Delta Engine: Skipping {len(live) - len(todo)} unchanged jobs. Fetching deep details for {len(todo)} jobs...")

    for slug in todo:
        path = os.path.join(PAGES_DIR, f"{slug}.html")
        job_info = live[slug]
        
        try:
            # Only POST and fetch from PPSC server if we don't have it saved locally, or if it changed
            if slug in redownload or not os.path.exists(path):
                html = fetch_detail_html(job_info)
                if html:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(html)
                    time.sleep(DELAY)
                else:
                    raise Exception("Failed to extract POST HTML")
            else:
                with open(path, encoding="utf-8") as f:
                    html = f.read()
            
            parsed_job = extract_job_details(html, slug, job_info)
            rows.append(parsed_job)
            
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

    # ---- Write to Supabase ----
    try:
        if rows:
            base.db_upsert(db, rows, tbl=TABLE)
        if gone:
            base.db_retire(db, gone, tbl=TABLE)
    except Exception as e:
        print(f"Supabase write FAILED: {e}")
        sys.exit(1)

    # Clean up cached files for retired jobs
    for slug in gone:
        print("OFF ", slug)
        p = os.path.join(PAGES_DIR, f"{slug}.html")
        if os.path.exists(p):
            os.remove(p)

    active_now = len((active - set(gone)) | {r["id"] for r in rows})
    print(f"\nNew: {c_new} | Reactivated: {c_back} | Updated: {c_upd} | "
          f"Removed: {len(gone)} | Failed: {failed} | Active now: {active_now}")
    
    if not (c_new or c_back or c_upd or gone or failed):
        print("No changes.")

if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv, force="--force" in sys.argv)