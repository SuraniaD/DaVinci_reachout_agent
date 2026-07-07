import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# Read credentials from environment variables
# These are set in Railway — never hardcoded here
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# Create one shared client — imported by all other files
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)