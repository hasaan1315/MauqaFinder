import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SECRET_KEY")
print("URL found:", bool(url), "| KEY found:", bool(key))

db = create_client(url, key)
print("Rows:", db.schema("raw").table("punjab_jobs_portal").select("id").limit(1).execute().data)