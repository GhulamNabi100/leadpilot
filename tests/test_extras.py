import unittest
from test_api import Base
CSV = "title,location,price,property_type,source\n1 Elm St,Austin TX,200000,house,Licensed CSV\n2 Elm St,Austin TX,abc,house,x\n1 Elm St,Austin TX,200000,house,x\n,Nowhere,1,house,x\n"

class T(Base):
    def seed(self, h): return self.c.post("/api/demo/seed", headers=h)
    def test_import_counts_and_errors(self):
        a = self.reg("A", "a@x.com"); r = self.c.post("/api/leads/import", data=CSV, content_type="text/csv", headers=a).get_json()
        self.assertEqual((r["created"], r["duplicates"], r["invalid"]), (1, 1, 2)); self.assertTrue(any("not a number" in e for e in r["errors"]))
    def test_import_rejects_bad_csv(self):
        a = self.reg("A", "a@x.com")
        self.assertEqual(self.c.post("/api/leads/import", data="foo,bar\n1,2", content_type="text/csv", headers=a).status_code, 400)
        big = "title\n" + "x\n" * 1001; self.assertEqual(self.c.post("/api/leads/import", data=big, content_type="text/csv", headers=a).status_code, 400)
    def test_seed_labelled_scored_and_rerun(self):
        a = self.reg("A", "a@x.com"); r = self.seed(a); self.assertEqual(r.status_code, 201)
        items = self.c.get("/api/leads?per_page=100", headers=a).get_json()["items"]
        self.assertGreaterEqual(len(items), 8); self.assertTrue(all(i["is_demo"] == 1 and i["score_detail"]["factors"] for i in items))
        wid = [w for w in self.c.get("/api/workflows", headers=a).get_json()["items"] if w["source"] == "demo"][0]["id"]
        self.assertEqual(self.c.post(f"/api/workflows/{wid}/run", json={}, headers=a).get_json()["status"], "success")
        self.assertGreater(self.c.get("/api/dashboard", headers=a).get_json()["total"], len(items))
        self.assertTrue(self.c.get("/api/notifications", headers=a).get_json()["items"])
    def test_unconnected_source_fails_honestly(self):
        a = self.reg("A", "a@x.com"); wid = self.c.post("/api/workflows", json={"name": "Zillow", "source": "zillow"}, headers=a).get_json()["id"]
        r = self.c.post(f"/api/workflows/{wid}/run", json={}, headers=a).get_json()
        self.assertEqual(r["status"], "failed"); self.assertIn("not connected", r["log"][0]["detail"])
        self.assertEqual(self.c.get("/api/dashboard", headers=a).get_json()["total"], 0)
        self.assertEqual(self.c.post(f"/api/executions/{r['id']}/retry", json={}, headers=a).get_json()["status"], "failed")
        self.assertEqual(self.c.get("/api/notifications", headers=a).get_json()["items"][0]["level"], "error")
    def test_disabled_and_retry_rules(self):
        a = self.reg("A", "a@x.com"); r = self.seed(a).get_json()
        self.assertEqual(self.c.post(f"/api/executions/{r['id']}/retry", json={}, headers=a).status_code, 400)  # success can't be retried
        wid = [w for w in self.c.get("/api/workflows", headers=a).get_json()["items"] if w["source"] == "demo"][0]["id"]
        self.c.patch(f"/api/workflows/{wid}", json={"enabled": False}, headers=a)
        self.assertEqual(self.c.post(f"/api/workflows/{wid}/run", json={}, headers=a).status_code, 409)
    def test_csv_workflow_requires_file(self):
        a = self.reg("A", "a@x.com"); self.seed(a)
        wid = [w for w in self.c.get("/api/workflows", headers=a).get_json()["items"] if w["source"] == "csv"][0]["id"]
        self.assertEqual(self.c.post(f"/api/workflows/{wid}/run", json={}, headers=a).get_json()["status"], "failed")
        self.assertEqual(self.c.post(f"/api/workflows/{wid}/run", json={"csv": CSV}, headers=a).get_json()["stats"]["created"], 1)
    def test_isolation_and_rbac(self):
        a, b = self.reg("A", "a@x.com"), self.reg("B", "b@x.com"); r = self.seed(a).get_json()
        wid = self.c.get("/api/workflows", headers=a).get_json()["items"][0]["id"]
        self.assertEqual(self.c.get("/api/workflows", headers=b).get_json()["items"], [])
        self.assertEqual(self.c.get("/api/executions", headers=b).get_json()["items"], [])
        self.assertEqual(self.c.get("/api/notifications", headers=b).get_json()["items"], [])
        self.assertEqual(self.c.post(f"/api/workflows/{wid}/run", json={}, headers=b).status_code, 404)
        self.assertEqual(self.c.patch(f"/api/workflows/{wid}", json={"enabled": False}, headers=b).status_code, 404)
        self.c.post("/api/users", json={"email": "v@x.com", "password": "viewer-pass-99", "role": "viewer"}, headers=a)
        v = {"Authorization": "Bearer " + self.c.post("/api/auth/login", json={"email": "v@x.com", "password": "viewer-pass-99"}).get_json()["token"]}
        self.assertEqual(self.seed(v).status_code, 403); self.assertEqual(self.c.post(f"/api/workflows/{wid}/run", json={}, headers=v).status_code, 403)
        self.assertEqual(self.c.post("/api/leads/import", data=CSV, content_type="text/csv", headers=v).status_code, 403)
    def test_ui_served_with_security_headers(self):
        r = self.c.get("/"); self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY"); self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(self.c.get("/static/app.js").status_code, 200)
if __name__ == "__main__": unittest.main()
