"""I4 browser features reuse exact existing controller-backed repository contracts."""
import unittest
from uuid import uuid4
from devstack.api.http import create_app
A, B, O = (str(uuid4()) for _ in range(3))
KEY="0123456789abcdef0123456789abcdef"

class RepoUIContractTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        def transport(op, **kwargs):
            self.calls.append((op,kwargs))
            if op == "repository_bulk_submit":
                return {"id":O,"project_id":A,"status":"queued","items":[]}
            if op == "repository_add_submit":
                return {"id":O,"project_id":A,"status":"queued","name":kwargs["name"]}
            if op == "branch_preview":
                return {"revision":"a"*64,"ready":True,"entries":[]}
            if op == "branch_apply":
                return {"complete":True,"results":[]}
            if op == "source_review":
                return {"revision":"b"*64,"changes":[]}
            if op == "source_apply":
                return {"applied":[]}
            if op == "repository_status":
                return {"head":"c"*40,"dirty":False}
            if op == "repository_update":
                return {"head":"d"*40,"dirty":False}
            if op == "repository_add_status":
                return {"id":O,"status":"running"}
            if op == "repository_bulk_status":
                return {"id":O,"status":"running"}
            raise AssertionError("Unexpected IPC: "+op)
        self.app=create_app({"TESTING":True,"SECRET_KEY":"test","SESSION_COOKIE_SECURE":False}, transport=transport)
        self.client=self.app.test_client()
        self.client.get("")
        with self.client.session_transaction() as s: self.csrf=s["_csrf"]

    def test_readonly_preview_does_not_mutate(self):
        urls=[
            f"/projects/{A}/repositories",
            f"/projects/{A}/repositories/{B}",
            f"/projects/{A}/repositories/branch",
            f"/projects/{A}/repositories/source",
            f"/projects/{A}/repositories/adds/{O}",
            f"/projects/{A}/repositories/updates/{O}",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(self.calls,[])

    def test_exact_guards_before_git_mutations(self):
        self.assertEqual(self.client.post(f"/api/v2/projects/{A}/git/branch-apply",
            json={"branch":"main","repository_id":B,"revision":"a"*64}).status_code,403)
        self.assertEqual(self.calls,[])
        headers={"X-CSRF-Token":self.csrf}
        self.assertEqual(self.client.post(f"/api/v2/projects/{A}/git/branch-apply",
            headers=headers,json={"branch":"main","repository_id":B,"revision":"a"*64,"extra":1}).status_code,400)
        self.assertEqual(self.client.post(f"/api/v2/projects/{A}/source-review/apply",
            headers=headers,json={"revision":"b"*64,"selected":["compose.yaml"]}).status_code,400)
        self.assertEqual(self.client.post(f"/api/v2/projects/{A}/repositories/{B}/update",
            headers=headers,json={"branch":"main"}).status_code,400)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.client.post(f"/api/v2/projects/{A}/git/branch-apply",
            headers=headers,json={"branch":"main","repository_id":B,"revision":"a"*64}).status_code,200)
        self.assertEqual(self.calls[-1],("branch_apply",{"project_id":A,"branch":"main","repository_id":B,"revision":"a"*64}))
        self.assertEqual(self.client.post(f"/api/v2/projects/{A}/source-review/apply",
            headers=headers,json={"revision":"b"*64,"selected":["compose.yaml"],"overwrites":[]}).status_code,200)
        self.assertEqual(self.calls[-1],("source_apply",{"project_id":A,"revision":"b"*64,
                                                      "selected":["compose.yaml"],"overwrites":[]}))
        self.assertEqual(self.client.post(f"/api/v2/projects/{A}/repositories/{B}/update",
            headers=headers,json={"expected_head":"c"*40}).status_code,200)
        self.assertEqual(self.calls[-1],("repository_update",{"project_id":A,"repository_id":B,"expected_head":"c"*40}))

    def test_async_add_bulk_require_csrf_and_idempotency(self):
        a=f"/api/v2/projects/{A}/repositories"
        bulk=f"/api/v2/projects/{A}/repositories/update-all"
        self.assertEqual(self.client.post(a,headers={"X-CSRF-Token":self.csrf},
            json={"url":"https://github.com/o/r","name":"repo"}).status_code,400)
        self.assertEqual(self.client.post(bulk,headers={"X-CSRF-Token":self.csrf},
            json={}).status_code,400)
        self.assertEqual(self.calls,[])
        headers={"X-CSRF-Token":self.csrf,"Idempotency-Key":KEY}
        self.assertEqual(self.client.post(a,headers=headers,json={
            "url":"https://github.com/o/r","name":"repo","branch":""}).status_code,202)
        self.assertEqual(self.calls[-1],("repository_add_submit",{"project_id":A,
            "url":"https://github.com/o/r","name":"repo","branch":"","request_key":KEY}))
        self.assertEqual(self.client.post(bulk,headers=headers,json={}).status_code,202)
        self.assertEqual(self.calls[-1],("repository_bulk_submit",{"project_id":A,"request_key":KEY}))

if __name__=="__main__":
    unittest.main()
