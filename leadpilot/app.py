import csv, io, json, os, re, sqlite3, time, uuid, hashlib, hmac
from datetime import datetime, timedelta, timezone
import jwt
from flask import Flask, g, jsonify, request, Response
from .scoring import score_lead
from .extras import register_extras

STATUSES = ["New", "Qualified", "Contacted", "Follow-up", "Interested", "Converted", "Rejected"]
ROLES = {"admin": 3, "agent": 2, "viewer": 1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs(id TEXT PRIMARY KEY, name TEXT NOT NULL, plan TEXT DEFAULT 'starter', lead_limit INTEGER DEFAULT 500, created_at TEXT);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES orgs(id), email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS leads(id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES orgs(id), name TEXT, email TEXT, phone TEXT,
 kind TEXT NOT NULL, title TEXT NOT NULL, source TEXT, location TEXT, price REAL, property_type TEXT, status TEXT NOT NULL DEFAULT 'New',
 priority TEXT DEFAULT 'medium', score INTEGER DEFAULT 0, score_detail TEXT, notes TEXT, assigned_to TEXT, data_quality TEXT DEFAULT 'estimated',
 is_demo INTEGER DEFAULT 0, dedupe_key TEXT NOT NULL, created_at TEXT, UNIQUE(org_id, dedupe_key));
CREATE INDEX IF NOT EXISTS ix_leads_org_status ON leads(org_id, status);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, lead_id TEXT, user_id TEXT, action TEXT, detail TEXT, at TEXT);
"""
now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())

def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16)
    return salt.hex() + ":" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()
def check_pw(pw, stored):
    salt, h = stored.split(":")
    return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), h)

def create_app(db_path=None, secret=None):
    app = Flask(__name__)
    app.config["DB"] = db_path or os.environ.get("LEADPILOT_DB", "leadpilot.db")
    app.config["SECRET"] = secret or os.environ.get("LEADPILOT_SECRET")
    if not app.config["SECRET"] or len(app.config["SECRET"]) < 32:
        raise RuntimeError("Set LEADPILOT_SECRET (>=32 chars). Never hard-code it.")
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000
    attempts = {}

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB"]); g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db
    @app.teardown_appcontext
    def close(_): 
        d = g.pop("db", None)
        if d: d.close()
    with sqlite3.connect(app.config["DB"]) as c: c.executescript(SCHEMA)

    def err(msg, code): return jsonify(error=msg), code
    def log(action, lead_id=None, detail=None):
        db().execute("INSERT INTO activity(org_id,lead_id,user_id,action,detail,at) VALUES(?,?,?,?,?,?)",
                     (g.user["org"], lead_id, g.user["uid"], action, detail, now()))

    def auth(min_role="viewer"):
        def deco(fn):
            def wrapper(*a, **k):
                h = request.headers.get("Authorization", "")
                try: g.user = jwt.decode(h.removeprefix("Bearer "), app.config["SECRET"], algorithms=["HS256"])
                except Exception: return err("Unauthorized", 401)
                if ROLES[g.user["role"]] < ROLES[min_role]: return err("Forbidden", 403)
                return fn(*a, **k)
            wrapper.__name__ = fn.__name__; return wrapper
        return deco

    def token(u):
        return jwt.encode({"uid": u["id"], "org": u["org_id"], "role": u["role"],
                           "exp": datetime.now(timezone.utc) + timedelta(hours=8)}, app.config["SECRET"], algorithm="HS256")

    def valid_creds(d):
        e, p = (d.get("email") or "").strip().lower(), d.get("password") or ""
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e): return None, "Invalid email"
        if len(p) < 10: return None, "Password must be at least 10 characters"
        return (e, p), None

    @app.post("/api/auth/register")
    def register():
        d = request.get_json(silent=True) or {}
        cr, e = valid_creds(d)
        if e or not (d.get("org_name") or "").strip(): return err(e or "org_name required", 400)
        oid, uid = str(uuid.uuid4()), str(uuid.uuid4())
        try:
            db().execute("INSERT INTO orgs(id,name,created_at) VALUES(?,?,?)", (oid, d["org_name"].strip()[:120], now()))
            db().execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (uid, oid, cr[0], hash_pw(cr[1]), "admin", now()))
            db().commit()
        except sqlite3.IntegrityError: db().rollback(); return err("Email already registered", 409)
        return jsonify(token=token({"id": uid, "org_id": oid, "role": "admin"})), 201

    @app.post("/api/auth/login")
    def login():
        d = request.get_json(silent=True) or {}
        key = (request.remote_addr, (d.get("email") or "").lower())
        n, t0 = attempts.get(key, (0, time.time()))
        if time.time() - t0 > 300: n, t0 = 0, time.time()
        if n >= 5: return err("Too many attempts; try later", 429)
        u = db().execute("SELECT * FROM users WHERE email=?", (key[1],)).fetchone()
        ok = check_pw(d.get("password") or "", u["pw"]) if u else (check_pw("x", hash_pw("y")) and False)
        if not ok: attempts[key] = (n + 1, t0); return err("Invalid credentials", 401)
        attempts.pop(key, None)
        return jsonify(token=token(u))

    @app.post("/api/users")
    @auth("admin")
    def add_user():
        d = request.get_json(silent=True) or {}
        cr, e = valid_creds(d)
        if e or d.get("role") not in ROLES: return err(e or "Invalid role", 400)
        try:
            db().execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), g.user["org"], cr[0], hash_pw(cr[1]), d["role"], now()))
            log("user.create", detail=f"{cr[0]} as {d['role']}"); db().commit()
        except sqlite3.IntegrityError: return err("Email already registered", 409)
        return jsonify(ok=True), 201

    def lead_out(r):
        o = dict(r); o["score_detail"] = json.loads(o["score_detail"] or "{}"); o.pop("dedupe_key"); return o

    @app.post("/api/leads")
    @auth("agent")
    def create_lead():
        return insert_lead(request.get_json(silent=True) or {})

    def insert_lead(d):
        title = (d.get("title") or "").strip()
        if not title or d.get("kind") not in ("property", "product"): return err("title and kind (property|product) required", 400)
        try: price = None if d.get("price") is None else float(d["price"])
        except (TypeError, ValueError): return err("price must be numeric", 400)
        if price is not None and price < 0: return err("price must be >= 0", 400)
        if db().execute("SELECT COUNT(*) FROM leads WHERE org_id=?", (g.user["org"],)).fetchone()[0] >= \
           db().execute("SELECT lead_limit FROM orgs WHERE id=?", (g.user["org"],)).fetchone()[0]:
            return err("Plan lead limit reached", 402)
        lead = {k: (str(d[k]).strip()[:300] if d.get(k) is not None else None) for k in
                ("name", "email", "phone", "source", "location", "property_type", "notes", "title")}
        lead.update(price=price, data_quality="verified" if d.get("data_quality") == "verified" else "estimated")
        sc = score_lead(lead, d.get("criteria"))
        lid = str(uuid.uuid4()); dk = norm(title) + "|" + norm(lead["location"])
        try:
            db().execute("""INSERT INTO leads(id,org_id,name,email,phone,kind,title,source,location,price,property_type,status,priority,score,
              score_detail,notes,data_quality,is_demo,dedupe_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'New',?,?,?,?,?,?,?,?)""",
              (lid, g.user["org"], lead["name"], lead["email"], lead["phone"], d["kind"], title, lead["source"], lead["location"], price,
               lead["property_type"], d.get("priority") if d.get("priority") in ("low", "medium", "high") else "medium", sc["score"],
               json.dumps(sc), lead["notes"], lead["data_quality"], 1 if d.get("is_demo") else 0, dk, now()))
        except sqlite3.IntegrityError:
            ex = db().execute("SELECT id FROM leads WHERE org_id=? AND dedupe_key=?", (g.user["org"], dk)).fetchone()
            return jsonify(error="Duplicate lead", existing_id=ex["id"]), 409
        log("lead.create", lid, title); db().commit()
        return jsonify(lead_out(db().execute("SELECT * FROM leads WHERE id=? AND org_id=?", (lid, g.user["org"])).fetchone())), 201

    def query_leads():
        q, args = "SELECT * FROM leads WHERE org_id=?", [g.user["org"]]
        if request.args.get("status") in STATUSES: q += " AND status=?"; args.append(request.args["status"])
        if request.args.get("search"):
            q += " AND (title LIKE ? OR name LIKE ? OR location LIKE ?)"; s = f"%{request.args['search']}%"; args += [s, s, s]
        if request.args.get("min_score", "").isdigit(): q += " AND score>=?"; args.append(int(request.args["min_score"]))
        sort = request.args.get("sort", "created_at"); sort = sort if sort in ("created_at", "score", "price", "status") else "created_at"
        return q + f" ORDER BY {sort} {'ASC' if request.args.get('order') == 'asc' else 'DESC'}", args

    @app.get("/api/leads")
    @auth()
    def list_leads():
        q, a = query_leads()
        page = max(int(request.args.get("page", 1)) if request.args.get("page", "1").isdigit() else 1, 1)
        per = min(int(request.args.get("per_page", 25)) if request.args.get("per_page", "25").isdigit() else 25, 100)
        total = db().execute(f"SELECT COUNT(*) FROM ({q})", a).fetchone()[0]
        rows = db().execute(q + " LIMIT ? OFFSET ?", a + [per, (page - 1) * per]).fetchall()
        return jsonify(items=[lead_out(r) for r in rows], total=total, page=page, per_page=per)

    @app.patch("/api/leads/<lid>")
    @auth("agent")
    def update_lead(lid):
        d = request.get_json(silent=True) or {}
        row = db().execute("SELECT * FROM leads WHERE id=? AND org_id=?", (lid, g.user["org"])).fetchone()
        if not row: return err("Not found", 404)
        if "status" in d and d["status"] not in STATUSES: return err("Invalid status", 400)
        if "assigned_to" in d and d["assigned_to"] and not db().execute("SELECT 1 FROM users WHERE id=? AND org_id=?", (d["assigned_to"], g.user["org"])).fetchone():
            return err("Assignee not in your organization", 400)
        for k in ("status", "priority", "notes", "assigned_to"):
            if k in d and d[k] != row[k]:
                if k == "priority" and d[k] not in ("low", "medium", "high"): return err("Invalid priority", 400)
                db().execute(f"UPDATE leads SET {k}=? WHERE id=? AND org_id=?", (d[k], lid, g.user["org"]))
                log(f"lead.{k}", lid, f"{row[k]} -> {d[k]}")
        db().commit()
        return jsonify(lead_out(db().execute("SELECT * FROM leads WHERE id=? AND org_id=?", (lid, g.user["org"])).fetchone()))

    @app.post("/api/leads/bulk-status")
    @auth("agent")
    def bulk_status():
        d = request.get_json(silent=True) or {}
        if d.get("status") not in STATUSES or not isinstance(d.get("ids"), list): return err("ids[] and valid status required", 400)
        n = 0
        for lid in d["ids"][:500]:
            if db().execute("UPDATE leads SET status=? WHERE id=? AND org_id=?", (d["status"], str(lid), g.user["org"])).rowcount:
                log("lead.status", str(lid), f"bulk -> {d['status']}"); n += 1
        db().commit(); return jsonify(updated=n)

    @app.get("/api/leads/<lid>/activity")
    @auth()
    def lead_activity(lid):
        rows = db().execute("SELECT action,detail,at,user_id FROM activity WHERE lead_id=? AND org_id=? ORDER BY id DESC", (lid, g.user["org"])).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.get("/api/leads/export.csv")
    @auth()
    def export_csv():
        q, a = query_leads(); out = io.StringIO(); w = csv.writer(out)
        cols = ["id", "name", "title", "source", "location", "price", "property_type", "status", "priority", "score", "data_quality", "is_demo", "created_at"]
        w.writerow(cols)
        for r in db().execute(q, a):
            w.writerow([("'" + str(r[c]) if isinstance(r[c], str) and r[c][:1] in "=+-@\t\r" else r[c]) for c in cols])  # CSV formula-injection guard
        log("leads.export"); db().commit()
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=leads.csv"})

    @app.get("/api/dashboard")
    @auth()
    def dashboard():
        o = g.user["org"]; by = {s: 0 for s in STATUSES}
        for r in db().execute("SELECT status, COUNT(*) c FROM leads WHERE org_id=? GROUP BY status", (o,)): by[r["status"]] = r["c"]
        src = {r["source"] or "unknown": r["c"] for r in db().execute("SELECT source, COUNT(*) c FROM leads WHERE org_id=? GROUP BY source", (o,))}
        today = db().execute("SELECT COUNT(*) FROM leads WHERE org_id=? AND created_at>=?", (o, datetime.now(timezone.utc).strftime("%Y-%m-%d"))).fetchone()[0]
        return jsonify(total=sum(by.values()), new_today=today, by_status=by, by_source=src)

    register_extras(app, db, auth, log, err, insert_lead)

    @app.errorhandler(404)
    def nf(_): return err("Not found", 404)
    @app.errorhandler(500)
    def ise(_): return err("Internal error", 500)  # no stack traces / internals leaked
    return app
