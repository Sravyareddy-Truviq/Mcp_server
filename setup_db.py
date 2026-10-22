"""
Supabase table setup script.
Run once to create tables for all three agents.
Usage: python setup_db.py
"""
import sys, os
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dns_patch  # noqa: F401 — must be first; patches socket before any network import

from dotenv import load_dotenv
from supabase import create_client

# Try loading from mcp-server/.env first, then root .env
load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

supabase_url = os.getenv("SUPABASE_URL") or os.getenv("supabase_url", "")
service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("service_role", "")

if not supabase_url or not service_key:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment/.env")

db = create_client(supabase_url.strip(), service_key.strip())

AGENTS = ["googleadk", "langchain", "langgraph"]

# We use Supabase REST API to execute raw SQL via rpc or just insert seed data.
# Tables must be created via Supabase dashboard SQL editor — see schema below.

SCHEMA_SQL = """
-- Run this in your Supabase SQL editor for each agent prefix:
"""

for agent in AGENTS:
    SCHEMA_SQL += f"""
-- {agent.upper()} tables
CREATE TABLE IF NOT EXISTS {agent}_accounts (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    balance NUMERIC(15,2) DEFAULT 0,
    currency TEXT DEFAULT 'USD',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {agent}_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_account TEXT REFERENCES {agent}_accounts(id),
    to_account TEXT,
    amount NUMERIC(15,2) NOT NULL,
    type TEXT NOT NULL,
    biller TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

print("=== Copy and run this SQL in your Supabase SQL Editor ===\n")
print(SCHEMA_SQL)

# Seed demo data
print("\n=== Seeding demo accounts ===")
for agent in AGENTS:
    try:
        db.table(f"{agent}_accounts").upsert([
            {"id": "ACC001", "owner": "Alice", "balance": 5000.00, "currency": "USD"},
            {"id": "ACC002", "owner": "Bob", "balance": 2500.00, "currency": "USD"},
            {"id": "ACC003", "owner": "Carol", "balance": 10000.00, "currency": "EUR"},
        ]).execute()
        print(f"  ✓ {agent} accounts seeded")
    except Exception as e:
        print(f"  ✗ {agent}: {e}")
