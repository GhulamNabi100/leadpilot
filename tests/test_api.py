import os, tempfile, unittest, csv, io
from leadpilot.app import create_app
from leadpilot.scoring import score_lead

class Base(unittest.TestCase):
    def setUp(self):
        self.f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.f.close()
        self.app = create_app(self.f.name, "x" * 40); self.c = self.app.test_client()
    def tearDown(self): os.unlink(self.f.name)
    def reg(self, org, email):
        r = self.c.post("/api/auth/register", json={"org_name": org, "email": email, "password": "correct-horse-9"})
        return {"Authorization": "Bearer " + r.get_json()["token"]}
    def lead(self, h, **kw):
        d = {"kind": "property", "title": "12 Oak St", "location": "Austin, TX", "price": 250000, "property_type": "house", "source": "csv"}; d.update(kw)
        return self.c.post("/api/leads", json=d, headers=h)

class TestAuth(Base):
    def test_register_login_and_validation(self):
        self.reg("A", "a@x.com")
        self.assertEqual(self.c.post("/api/auth/login", json={"email": "a@x.com", "password": "correct-horse-9"}).status_code, 200)
        self.assertEqual(self.c.post("/api/auth/login", json={"email": "a@x.com", "password": "nope"}).status_code, 401)
        self.assertEqual(self.c.post("/api/auth/register", json={"org_name": "B", "email": "bad", "password": "correct-horse-9"}).status_code, 400)
        self.assertEqual(self.c.post("/api/auth/register", json={"org_name": "B", "email": "b@x.com", "password": "short"}).status_code, 400)
        self.assertEqual(self.c.post("/api/auth/register", json={"org_name": "A2", "email": "a@x.com", "password": "correct-horse-9"}).status_code, 409)
    def test_no_token_and_rate_limit(self):
        self.assertEqual(self.c.get("/api/leads").status_code, 401)
        self.assertEqual(self.c.get("/api/leads", headers={"Authorization": "Bearer junk"}).status_code, 401)
        self.reg("A", "a@x.com")
        codes = [self.c.post("/api/auth/login", json={"email": "a@x.com", "password": "bad"}).status_code for _ in range(7)]
        self.assertEqual(codes[-1], 429)
    def test_password_not_stored_plain(self):
        import sqlite3; self.reg("A", "a@x.com")
        pw = sqlite3.connect(self.f.name).execute("SELECT pw FROM users").fetchone()[0]
        self.assertNotIn("correct-horse-9", pw)

class TestIsolationAndRBAC(Base):
    def test_cross_org_isolation(self):
        a, b = self.reg("A", "a@x.com"), self.reg("B", "b@x.com")
        lid = self.lead(a).get_json()["id"]
        self.assertEqual(self.c.get("/api/leads", headers=b).get_json()["total"], 0)
        self.assertEqual(self.c.patch(f"/api/leads/{lid}", json={"status": "Qualified"}, headers=b).status_code, 404)
        self.assertEqual(self.c.get(f"/api/leads/{lid}/activity", headers=b).get_json()["items"], [])
        self.assertEqual(self.c.post("/api/leads/bulk-status", json={"ids": [lid], "status": "Rejected"}, headers=b).get_json()["updated"], 0)
        self.assertEqual(self.c.get("/api/leads", headers=a).get_json()["items"][0]["status"], "New")
        self.assertNotIn(lid, self.c.get("/api/leads/export.csv", headers=b).get_data(as_text=True))
        self.assertEqual(self.lead(b).status_code, 201)  # same address is NOT a duplicate across orgs
    def test_viewer_cannot_write(self):
        a = self.reg("A", "a@x.com")
        self.c.post("/api/users", json={"email": "v@x.com", "password": "viewer-pass-99", "role": "viewer"}, headers=a)
        t = self.c.post("/api/auth/login", json={"email": "v@x.com", "password": "viewer-pass-99"}).get_json()["token"]
        v = {"Authorization": "Bearer " + t}
        self.assertEqual(self.lead(v).status_code, 403)
        self.assertEqual(self.c.post("/api/users", json={"email": "z@x.com", "password": "viewer-pass-99", "role": "admin"}, headers=v).status_code, 403)
        self.assertEqual(self.c.get("/api/leads", headers=v).status_code, 200)
    def test_cannot_assign_foreign_user(self):
        a, b = self.reg("A", "a@x.com"), self.reg("B", "b@x.com")
        import sqlite3; foreign = sqlite3.connect(self.f.name).execute("SELECT id FROM users WHERE email='b@x.com'").fetchone()[0]
        lid = self.lead(a).get_json()["id"]
        self.assertEqual(self.c.patch(f"/api/leads/{lid}", json={"assigned_to": foreign}, headers=a).status_code, 400)

class TestCRM(Base):
    def test_duplicate_detection(self):
        a = self.reg("A", "a@x.com"); first = self.lead(a).get_json()
        r = self.lead(a, title="12  OAK st.", location="austin tx")
        self.assertEqual(r.status_code, 409); self.assertEqual(r.get_json()["existing_id"], first["id"])
    def test_validation(self):
        a = self.reg("A", "a@x.com")
        self.assertEqual(self.lead(a, title="").status_code, 400)
        self.assertEqual(self.lead(a, kind="car").status_code, 400)
        self.assertEqual(self.lead(a, price="abc").status_code, 400)
        self.assertEqual(self.lead(a, price=-5).status_code, 400)
    def test_status_workflow_activity_bulk_filter_paging(self):
        a = self.reg("A", "a@x.com"); ids = [self.lead(a, title=f"P{i}").get_json()["id"] for i in range(3)]
        self.assertEqual(self.c.patch(f"/api/leads/{ids[0]}", json={"status": "Bogus"}, headers=a).status_code, 400)
        self.c.patch(f"/api/leads/{ids[0]}", json={"status": "Qualified"}, headers=a)
        self.assertEqual(self.c.get(f"/api/leads/{ids[0]}/activity", headers=a).get_json()["items"][0]["detail"], "New -> Qualified")
        self.assertEqual(self.c.post("/api/leads/bulk-status", json={"ids": ids[1:], "status": "Contacted"}, headers=a).get_json()["updated"], 2)
        self.assertEqual(self.c.get("/api/leads?status=Contacted", headers=a).get_json()["total"], 2)
        self.assertEqual(len(self.c.get("/api/leads?per_page=2&page=2", headers=a).get_json()["items"]), 1)
        self.assertEqual(self.c.get("/api/leads?sort=price;DROP TABLE leads", headers=a).status_code, 200)  # sort whitelisted
        self.assertEqual(self.c.get("/api/leads?search=' OR 1=1 --", headers=a).get_json()["total"], 0)      # parameterized
        d = self.c.get("/api/dashboard", headers=a).get_json(); self.assertEqual((d["total"], d["by_status"]["Contacted"]), (3, 2))
    def test_csv_export_formula_injection_guard(self):
        a = self.reg("A", "a@x.com"); self.lead(a, title="=HYPERLINK(\"http://evil\")", location="X")
        rows = list(csv.reader(io.StringIO(self.c.get("/api/leads/export.csv", headers=a).get_data(as_text=True))))
        self.assertTrue(rows[1][2].startswith("'="))
    def test_plan_limit(self):
        import sqlite3; a = self.reg("A", "a@x.com")
        sqlite3.connect(self.f.name).execute("UPDATE orgs SET lead_limit=1").connection.commit()
        self.assertEqual(self.lead(a, title="one").status_code, 201); self.assertEqual(self.lead(a, title="two").status_code, 402)

class TestScoring(unittest.TestCase):
    crit = {"locations": ["Austin"], "min_price": 100000, "max_price": 300000, "property_types": ["house"]}
    def test_full_match_and_explanation(self):
        r = score_lead({"location": "Austin, TX", "price": 250000, "property_type": "house", "data_quality": "verified"}, self.crit)
        self.assertEqual(r["score"], 100); self.assertTrue(all(f["basis"] == "verified" for f in r["factors"]))
    def test_missing_data_not_invented(self):
        r = score_lead({"location": "Austin", "price": None, "property_type": None}, self.crit)
        self.assertEqual(r["score"], 30); self.assertEqual(sorted(r["unknown_fields"]), ["price", "type"])
        self.assertTrue(all(f["basis"] in ("estimated", "unknown") for f in r["factors"]))
    def test_mismatch(self):
        self.assertEqual(score_lead({"location": "Reno", "price": 900000, "property_type": "condo"}, self.crit)["score"], 0)

if __name__ == "__main__": unittest.main(verbosity=2)
