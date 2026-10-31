import csv, io, json, random, sqlite3, uuid
from datetime import datetime, timezone
from flask import request, jsonify, g, send_from_directory
import os

EXTRA = """
CREATE TABLE IF NOT EXISTS workflows(id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL, criteria TEXT, enabled INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY, org_id TEXT NOT NULL, workflow_id TEXT NOT NULL, status TEXT, started TEXT, finished TEXT, stats TEXT, log TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, message TEXT, level TEXT, at TEXT, read INTEGER DEFAULT 0);
"""
LIVE = {"demo", "csv"}  # actually functional sources
NOT_CONNECTED = {"zillow", "auction.com", "propwire", "amazon", "daraz"}  # need official API / licensed access
now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
STATIC = os.path.join(os.path.dirname(__file__), "static")

def parse_csv(text):
    rd = csv.DictReader(io.StringIO((text or "").lstrip("\ufeff")))
    if not rd.fieldnames or "title" not in [f.strip().lower() for f in rd.fieldnames if f]:
        raise ValueError("CSV must have a header row with a 'title' column")
    rows = []
    for r in rd:
        rows.append(r)
        if len(rows) > 1000: raise ValueError("CSV limited to 1000 rows per import")
    return rows

def clean(r):
    r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items() if k}
    if not r.get("title"): return None, "title is empty"
    price = r.get("price", "").replace("$", "").replace(",", "") or None
    if price is not None:
        try: price = float(price)
        except ValueError: return None, f"price '{r['price']}' is not a number"
    d = {k: r.get(k) or None for k in ("name", "email", "phone", "location", "property_type", "source", "notes", "data_quality")}
    d.update(title=r["title"], price=price, kind=r.get("kind") or "property")
    return d, None

def demo_rows(seed):
    rng = random.Random(seed)
    st, ci = ["Oak St", "Maple Ave", "Cedar Ln", "Pine Rd", "Elm Dr", "Lakeview Blvd"], ["Austin, TX", "Phoenix, AZ", "Tampa, FL", "Atlanta, GA", "Denver, CO"]
    return [{"title": f"{rng.randint(100, 999)} {rng.choice(st)}", "location": rng.choice(ci), "price": str(rng.randint(80, 650) * 1000),
             "property_type": rng.choice(["house", "condo", "townhouse", "land"]),
             "source": rng.choice(["Demo: Auction feed", "Demo: Public listings", "Demo: Pre-foreclosure"]), "name": f"Demo Contact {i + 1}"} for i in range(10)]

def register_extras(app, db, auth, log, err, insert_lead):
    with sqlite3.connect(app.config["DB"]) as c: c.executescript(EXTRA)

    def process(rows, crit, is_demo):
        st, errs, limit = {"created": 0, "duplicates": 0, "invalid": 0}, [], False
        for i, r in enumerate(rows, 2):
            d, e = clean(r)
            if e: st["invalid"] += 1; errs.append(f"row {i}: {e}"); continue
            d.update(criteria=crit, is_demo=is_demo)
            resp, code = insert_lead(d)
            if code == 201: st["created"] += 1
            elif code == 409: st["duplicates"] += 1
            elif code == 402: limit = True; break
            else: st["invalid"] += 1; errs.append(f"row {i}: {resp.get_json()['error']}")
        return st, errs[:20], limit

    def notify(msg, level):
        db().execute("INSERT INTO notifications(org_id,message,level,at) VALUES(?,?,?,?)", (g.user["org"], msg, level, now()))

    def run_workflow(wf, csv_text=None):
        eid, t0, steps, stats, status = str(uuid.uuid4()), now(), [], {}, "success"
        try:
            crit = json.loads(wf["criteria"]) if wf["criteria"] else None
            if wf["source"] == "demo":
                n = db().execute("SELECT COUNT(*) FROM executions WHERE org_id=?", (g.user["org"],)).fetchone()[0]
                rows = demo_rows(n)
            elif wf["source"] == "csv":
                if not csv_text: raise ValueError("This workflow needs a CSV file; none was provided")
                rows = parse_csv(csv_text)
            else:
                raise ValueError(f"Source '{wf['source']}' is not connected. It needs official API credentials or licensed access; use a CSV-source workflow with an authorized export instead.")
            steps.append({"step": "Source", "ok": True, "detail": f"{len(rows)} rows read"})
            stats, errs, limit = process(rows, crit, wf["source"] == "demo")
            steps += [{"step": "Validation", "ok": True, "detail": f"{stats['invalid']} invalid" + (": " + "; ".join(errs) if errs else "")},
                      {"step": "Duplicate detection", "ok": True, "detail": f"{stats['duplicates']} duplicates skipped"},
                      {"step": "Scoring + save", "ok": not limit, "detail": f"{stats['created']} leads created" + (" (plan lead limit reached)" if limit else "")}]
            if limit: status = "partial"
            notify(f"Workflow '{wf['name']}' {status}: {stats['created']} new, {stats['duplicates']} duplicates", "info" if status == "success" else "warning")
        except ValueError as e:
            status = "failed"; steps.append({"step": "Source", "ok": False, "detail": str(e)})
            notify(f"Workflow '{wf['name']}' failed: {e}", "error")
        db().execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)", (eid, g.user["org"], wf["id"], status, t0, now(), json.dumps(stats), json.dumps(steps)))
        log("workflow.run", None, f"{wf['name']} -> {status}"); db().commit()
        return {"id": eid, "status": status, "stats": stats, "log": steps}

    def get_wf(wid): return db().execute("SELECT * FROM workflows WHERE id=? AND org_id=?", (wid, g.user["org"])).fetchone()
    def new_wf(name, source, crit):
        wid = str(uuid.uuid4())
        db().execute("INSERT INTO workflows VALUES(?,?,?,?,?,1,?)", (wid, g.user["org"], name, source, json.dumps(crit) if crit else None, now())); return wid

    @app.post("/api/leads/import")
    @auth("agent")
    def x_import():
        text = (request.get_json(silent=True) or {}).get("csv") if request.is_json else request.get_data(as_text=True)
        try: rows = parse_csv(text)
        except ValueError as e: return err(str(e), 400)
        st, errs, limit = process(rows, None, False)
        log("leads.import", None, json.dumps(st)); db().commit()
        return jsonify(**st, errors=errs, limit_reached=limit)

    @app.post("/api/demo/seed")
    @auth("admin")
    def x_seed():
        crit = {"locations": ["Austin", "Tampa"], "min_price": 100000, "max_price": 400000, "property_types": ["house", "townhouse"]}
        ex = db().execute("SELECT * FROM workflows WHERE org_id=? AND name='Demo property feed'", (g.user["org"],)).fetchone()
        if not ex:
            new_wf("Licensed CSV import", "csv", crit); wid = new_wf("Demo property feed", "demo", crit); db().commit(); ex = get_wf(wid)
        return jsonify(run_workflow(ex)), 201

    @app.get("/api/workflows")
    @auth()
    def wf_list():
        return jsonify(items=[dict(r) | {"criteria": json.loads(r["criteria"]) if r["criteria"] else None, "enabled": bool(r["enabled"]), "live_source": r["source"] in LIVE}
                              for r in db().execute("SELECT * FROM workflows WHERE org_id=? ORDER BY created_at", (g.user["org"],))])

    @app.post("/api/workflows")
    @auth("admin")
    def wf_create():
        d = request.get_json(silent=True) or {}; name, src = (d.get("name") or "").strip()[:120], d.get("source")
        if not name or src not in LIVE | NOT_CONNECTED: return err("name and a valid source required", 400)
        wid = new_wf(name, src, d.get("criteria") if isinstance(d.get("criteria"), dict) else None); db().commit()
        return jsonify(id=wid), 201

    @app.patch("/api/workflows/<wid>")
    @auth("admin")
    def wf_patch(wid):
        if not get_wf(wid): return err("Not found", 404)
        en = (request.get_json(silent=True) or {}).get("enabled")
        if not isinstance(en, bool): return err("enabled (bool) required", 400)
        db().execute("UPDATE workflows SET enabled=? WHERE id=? AND org_id=?", (int(en), wid, g.user["org"])); db().commit()
        return jsonify(ok=True)

    @app.post("/api/workflows/<wid>/run")
    @auth("agent")
    def wf_run(wid):
        wf = get_wf(wid)
        if not wf: return err("Not found", 404)
        if not wf["enabled"]: return err("Workflow is disabled", 409)
        return jsonify(run_workflow(wf, (request.get_json(silent=True) or {}).get("csv")))

    @app.get("/api/executions")
    @auth()
    def ex_list():
        rows = db().execute("""SELECT e.*, w.name AS workflow_name, w.source FROM executions e JOIN workflows w ON w.id=e.workflow_id
                               WHERE e.org_id=? ORDER BY e.started DESC, e.rowid DESC LIMIT 50""", (g.user["org"],))
        return jsonify(items=[dict(r) | {"stats": json.loads(r["stats"] or "{}"), "log": json.loads(r["log"] or "[]")} for r in rows])

    @app.post("/api/executions/<eid>/retry")
    @auth("agent")
    def ex_retry(eid):
        e = db().execute("SELECT * FROM executions WHERE id=? AND org_id=?", (eid, g.user["org"])).fetchone()
        if not e: return err("Not found", 404)
        if e["status"] == "success": return err("Only failed or partial runs can be retried", 400)
        wf = get_wf(e["workflow_id"])
        if not wf["enabled"]: return err("Workflow is disabled", 409)
        return jsonify(run_workflow(wf, (request.get_json(silent=True) or {}).get("csv")))

    @app.get("/api/notifications")
    @auth()
    def nt_list():
        rows = db().execute("SELECT id,message,level,at,read FROM notifications WHERE org_id=? ORDER BY id DESC LIMIT 50", (g.user["org"],))
        return jsonify(items=[dict(r) for r in rows])

    @app.post("/api/notifications/read")
    @auth()
    def nt_read():
        db().execute("UPDATE notifications SET read=1 WHERE org_id=?", (g.user["org"],)); db().commit(); return jsonify(ok=True)

    @app.get("/")
    def index(): return send_from_directory(STATIC, "index.html")

    @app.after_request
    def sec_headers(r):
        r.headers.update({"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'", "X-Content-Type-Options": "nosniff",
                          "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store" if request.path.startswith("/api") else "no-cache"})
        return r
