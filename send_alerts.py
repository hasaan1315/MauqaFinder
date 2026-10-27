import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

# We use the Service Role / Secret Key to bypass RLS and read all user profiles
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY"))

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

def get_new_jobs():
    """Fetch jobs scraped or updated today across all portals."""
    today = datetime.utcnow().date().isoformat()
    all_new_jobs = []
    
    for table in ["ppsc_jobs", "njp_jobs", "pjp_jobs"]:
        try:
            # Assumes your database has a created_at or updated_at column
            res = supabase.table(table).select("*").gte("created_at", today).execute()
            if res.data:
                all_new_jobs.extend(res.data)
        except Exception as e:
            print(f"Could not fetch from {table}: {e}")
            
    return all_new_jobs

def get_matching_users(job):
    """Filter the user_profiles table based on job requirements."""
    query = supabase.table("user_profiles").select("id, email, full_name")
    
    # 1. Match Experience
    required_exp = job.get('experience_years') or 0
    query = query.gte("experience_years", required_exp)
    
    # 2. Match Location (if specified)
    posting = job.get('place_of_posting')
    if posting and posting.lower() not in ["punjab", "anywhere", "multiple", "all over pakistan"]:
        query = query.ilike("preferred_district", f"%{posting}%")
        
    res = query.execute()
    return res.data

def main():
    jobs = get_new_jobs()
    if not jobs:
        print("No new jobs today. Skipping alerts.")
        return

    print(f"Found {len(jobs)} new jobs. Cross-referencing with user profiles...")
    
    # Open the Gmail SMTP connection ONCE for all emails (much faster)
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(GMAIL_USER, GMAIL_PASSWORD)
    except Exception as e:
        print(f"Failed to connect to Gmail SMTP: {e}")
        return

    emails_sent = 0

    for job in jobs:
        users = get_matching_users(job)
        for user in users:
            try:
                msg = EmailMessage()
                msg['Subject'] = f"🚨 New Job Match: {job['title']} at {job['department']}"
                msg['From'] = f"MauqaFinder <{GMAIL_USER}>"
                msg['To'] = user['email']
                
                # The email body
                content = f"Hi {user.get('full_name', 'there')},\n\n"
                content += f"We found a new government job matching your profile!\n\n"
                content += f"Job Title: {job['title']}\n"
                content += f"Department: {job.get('department', 'Government Department')}\n"
                content += f"Required Experience: {job.get('experience_years', 0)} years\n\n"
                content += f"Apply before it closes: {job.get('closing_date', 'ASAP')}\n\n"
                content += f"Log in to MauqaFinder to view more details:\n"
                content += f"https://mauqa-finder-app.vercel.app/\n\n"
                content += "Good luck!\n- The MauqaFinder Engine"
                
                msg.set_content(content)
                server.send_message(msg)
                emails_sent += 1
                
            except Exception as e:
                print(f"Failed to send email to {user['email']}: {e}")

    server.quit()
    print(f"✅ Alert sync complete! Successfully sent {emails_sent} matching emails.")

if __name__ == "__main__":
    main()