"""I7 native project create/import navigation must never execute on GET."""
import unittest
from uuid import uuid4
from devstack.api.http import create_app
P=str(uuid4())
KEY="a"*32

class ProjectManagementUITests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        def transport(op,**fields):
            self.calls.append((op,fields))
            if op=="project_create":return {"id":P,"name":fields["name"]}
            if op=="import_submit":return {"id":P,"slug":fields["slug"],"name":fields["name"],"status":"queued"}
            if op=="import_status":return {"id":P,"slug":"demo","name":"Demo","status":"running","project_id":None}
            raise AssertionError(op)
        self.app=create_app({"TESTING":True,"SECRET_KEY":"test","SESSION_COOKIE_SECURE":False},transport=transport)
        self.client=self.app.test_client()
        self.client.get("")
        with self.client.session_transaction() as session:self.csrf=session["_csrf"]
    def test_deep_links_are_contained_and_side_effect_free(self):
        for url in ("/projects/create","/projects/import",f"/imports/{P}"):
            self.assertEqual(self.client.get(url).status_code,200)
        for url in ("/imports/invalid","/projects/create/shell",
                    f"/imports/{P}/logs"):
            self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.calls,[])
    def test_create_requires_csrf_and_exact_payload(self):
        path="/api/v2/projects"
        self.assertEqual(self.client.post(path,json={"slug":"demo","name":"Demo"}).status_code,403)
        headers={"X-CSRF-Token":self.csrf}
        self.assertEqual(self.client.post(path,headers=headers,json={"slug":"demo","name":"Demo","extra":True}).status_code,400)
        self.assertEqual(self.calls,[])
        r=self.client.post(path,headers=headers,json={"slug":"demo","name":"Demo"})
        self.assertEqual(r.status_code,201)
        self.assertEqual(self.calls,[("project_create",{"slug":"demo","name":"Demo"})])
    def test_import_has_durable_location_and_requires_idempotency(self):
        payload={"url":"https://github.com/org/project","slug":"demo","name":"Demo"}
        path="/api/v2/projects/import"
        headers={"X-CSRF-Token":self.csrf}
        self.assertEqual(self.client.post(path,headers=headers,json=payload).status_code,400)
        self.assertEqual(self.calls,[])
        headers["Idempotency-Key"]=KEY
        result=self.client.post(path,headers=headers,json=payload)
        self.assertEqual(result.status_code,202)
        self.assertEqual(result.get_json()["import"]["id"],P)
        self.assertIn(P,result.headers["Location"])
        self.assertEqual(self.calls[-1],("import_submit",{**payload,"request_key":KEY}))
        self.assertEqual(self.client.get("/api/v2/imports/"+P).status_code,200)
        self.assertEqual(self.calls[-1],("import_status",{"id":P}))
if __name__=="__main__":unittest.main()
