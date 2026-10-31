# LeadPilot AI (working prototype, v0.2)

Multi-tenant lead CRM + explainable lead scoring + workflow engine, with a browser UI.
Stack actually used: Python 3.12, Flask, SQLite (stdlib), PyJWT, vanilla JS. (The original brief preferred Next.js/FastAPI/Postgres; those could not be installed in the build sandbox, so they were NOT used or tested.)

## Run
    python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
    export LEADPILOT_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")   # required, >=32 chars, never commit
    python3 run.py          # http://127.0.0.1:5000
    python3 -m unittest discover -s tests -v

## Demo access
There is deliberately NO built-in demo password. Open the site, click "Create workspace" (email, password of 10+ chars, org name),
then Workflows -> "Load demo data". Demo leads are synthetic and flagged DEMO.

## What works (tested)
Auth (scrypt, JWT 8h, login rate limit) · roles admin/agent/viewer · org isolation · lead CRM (7 statuses, search, filter, sort, paging, bulk status,
activity log, duplicate detection, CSV export with formula-injection guard) · CSV import (validation report) · explainable scoring (verified/estimated/unknown) ·
workflow engine (demo + CSV sources, enable/disable, run, history, retry, notifications) · per-org lead limit (plan hook).

## NOT built / not connected
Zillow, Auction.com, Propwire, Amazon, Daraz (need official/licensed access; such workflows fail with a clear "not connected" message — use CSV exports) ·
Google Sheets · e-commerce price tracking module · scheduling · email notifications · billing/payments · user-management/settings/billing UI pages ·
PostgreSQL + migrations · light mode · dependency vulnerability scan · penetration testing.
Not production-ready: use a production WSGI server + HTTPS, and move rate limiting/sessions to shared storage before multi-instance deployment.
