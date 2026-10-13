"""
sync_ppsc.py - keep the Supabase table raw.ppsc_jobs in sync with PPSC active jobs.

Handles ASP.NET __VIEWSTATE form submissions to click into individual job detail
pages and extract deep information like Qualifications, Vacancies, and Eligibility.
"""
import os
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import sync_pjp as base  # shared Supabase database helpers

START_URL = "https://www.ppsc.gop.pk/Jobs.aspx"
TABLE = "ppsc_jobs"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
DELAY = 3
RETRIES = 2

session = requests.Session()
session.headers.update(HEADERS)


def get_html(url, method="GET", data=None):
    """Download page with retry logic for dropped connections."""
    for attempt in range(RETRIES + 1):
        try:
            if method == "POST":
                r = session.post(url, data=data, timeout=30, allow_redirects=True)
            else:
                r = session.get(url, timeout=30, allow_redirects=True)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            print(f"  failed: {type(e).__name__}")
            if attempt < RETRIES:
                print(f"  retry {attempt + 1}/{RETRIES}...")
                time.sleep(10)
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
    # This effortlessly handles "3 year's post-bachelor's degree practical experience"
    match = re.search(r"(\d+)\s*(?:years?|yrs?)['\s]*s?(?:\s+[\w\'-]+){0,10}\s+experience", clean_text)
    
    if match:
        return int(match.group(1))
        
    # Fallback search if the word 'years' comes way later
    fallback_match = re.search(r"(\d+)\s*(?:[\w\s,\'-]+){0,15}years?['\s]*s?\s+experience", clean_text)
    return int(fallback_match.group(1)) if fallback_match else 0

def extract_job_details(html, case_no, title, department, fee, closing_date, url):
    """Parses the deep detail page for rich fields."""
    soup = BeautifulSoup(html, "lxml")
    
    for t in soup(["script", "style"]):
        t.decompose()
        
    text = soup.get_text(" ", strip=True)
    
    # 1. Vacancies
    vac_match = re.search(r'Total Number of Posts:\s*(\d+)', text, re.I)
    vacancies = int(vac_match.group(1)) if vac_match else 1

    # 2. Grade / Pay Scale
    grade_match = re.search(r'(BS-\d+|BPS-\d+)', text, re.I)
    grade = grade_match.group(1).upper() if grade_match else None

    # 3. Place of Posting
    posting_match = re.search(r'Place Of Posting:\s*([A-Za-z\s,]+?)(?:Additional Requirement|Research|Preferences|Fee|$)', text, re.I)
    place_of_posting = posting_match.group(1).strip() if posting_match else "Punjab"

    # 4. Eligibility
    elig_match = re.search(r'Eligibililty:\s*([^\n]+?)(?:Place Of Posting|$)', text, re.I)
    eligibility = elig_match.group(1).strip() if elig_match else None

    # 5. Qualification Text
    qual_match = re.search(r'Qualification:\s*(.*?)(?:Total Number of Posts|Experience:|Pay Scale:|$)', text, re.I | re.DOTALL)
    if qual_match:
        qualification_text = qual_match.group(1).strip()
        # Clean up weird leading bullets/parentheses and normalize "Graduation" for SQL parsing
        qualification_text = re.sub(r'^[\)\(\]\}\s*]+', '', qualification_text)
        qualification_text = re.sub(r'\bGraduation\b', "Bachelor's Degree", qualification_text, flags=re.I)
    else:
        qualification_text = None

    # Experience Years via Parser helper
    experience_years = parse_experience(qualification_text)

    # 6. Age Parsing (Extracts min and max with gender relaxations)
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

    record_id = re.sub(r'[^a-zA-Z0-9]', '_', case_no).lower()

    return {
        "id": record_id,
        "title": title,
        "department": department,
        "grade": grade,
        "vacancies": vacancies,
        "description": f"{title} at {department}. Case No: {case_no}",
        "qualification_text": qualification_text,
        "experience_years": experience_years,
        "eligibility": eligibility,
        "place_of_posting": place_of_posting,
        "fee": fee,
        "closing_date": closing_date,
        "url": url,
        "age_min": age_min,
        "age_max_male": age_max_male,
        "age_max_female": age_max_female,
        "is_active": True
    }


def fetch_jobs():
    """Scrapes the main list, then deeply scrapes each job's detail page via ASP.NET postbacks."""
    print("Fetching PPSC Jobs Table...")
    res = get_html(START_URL)
    if not res:
        sys.exit("Failed to reach PPSC landing page.")
    
    session_url = res.url 
    soup = BeautifulSoup(res.text, 'lxml')
    live_jobs = {}

    rows = soup.select("table tr")
    job_targets = []

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
            job_targets.append({
                "case_no": case_no,
                "title": title,
                "department": department,
                "fee": fee,
                "closing_date": closing_date,
                "target": target_id
            })

    print(f"Found {len(job_targets)} jobs. Fetching detail pages...")

    for job in job_targets:
        print(f" -> Fetching details for: {job['case_no']} | {job['title'][:30]}...")
        
        fresh_res = get_html(session_url)
        fresh_soup = BeautifulSoup(fresh_res.text, 'lxml')
        
        viewstate = fresh_soup.select_one("#__VIEWSTATE")
        eventval = fresh_soup.select_one("#__EVENTVALIDATION")
        
        if not viewstate:
            continue

        form_data = {
            "__EVENTTARGET": job["target"],
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate.get("value", ""),
            "__EVENTVALIDATION": eventval.get("value", "") if eventval else ""
        }

        detail_res = get_html(session_url, method="POST", data=form_data)
        if detail_res:
            parsed_job = extract_job_details(
                detail_res.text, 
                job["case_no"], 
                job["title"], 
                job["department"], 
                job["fee"], 
                job["closing_date"], 
                session_url
            )
            if parsed_job:
                live_jobs[parsed_job["id"]] = parsed_job
            else:
                print(f"    WARNING: Could not parse details for {job['case_no']}")
        
        time.sleep(DELAY)

    return live_jobs


def main(force=False):
    live = fetch_jobs()
    print(f"\nSuccessfully parsed {len(live)} detailed PPSC jobs.")
    
    if not live:
        sys.exit("No jobs fully parsed. Stopping.")

    db = base.get_db()
    try:
        saved = base.db_read(db, "id,is_active", tbl=TABLE)
    except Exception as e:
        sys.exit(f"Supabase connection error for {TABLE}: {e}")

    known = {r["id"]: r["is_active"] for r in saved}
    active = {i for i, k in known.items() if k}

    new = [s for s in live if s not in known]
    back = [s for s in live if s in known and not known[s]]
    gone = [s for s in active if s not in live]

    if active and len(gone) > max(3, len(active) // 2) and not force:
        sys.exit(f"Would remove {len(gone)} PPSC jobs. Run with --force if intentional.")

    rows = [live[job_id] for job_id in list(live)]
    
    try:
        base.db_upsert(db, rows, tbl=TABLE)
        base.db_retire(db, gone, tbl=TABLE)
    except Exception as e:
        sys.exit(f"Supabase write FAILED: {e}")

    print(f"New: {len(new)} | Reactivated: {len(back)} | Removed: {len(gone)}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)