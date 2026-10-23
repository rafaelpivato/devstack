"""I6 preview and reviewed settings API safety."""
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from devstack.api.http import create_app
from devstack.projects.configuration import ConfigurationConflict, ConfigurationService

PROJECT=str(uuid4())
REV="a"*64

class ConfigurationApiTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        def transport(op,**fields):
            self.calls.append((op,fields))
            if op=="settings_review":
                return {"applications":[],"tasks":[],"revision":REV}
            if op=="settings_replace_checked":
                return {"applications":fields["applications"],"tasks":fields["tasks"]}
            if op=="compose_read":
                return {"content":"services: {}","revision":"none"}
            if op=="webhooks":return {"webhooks":[]}
            if op=="github_ssh_status":return {"public_key":None}
            if op=="project_detail":return {"project":{"id":PROJECT,"name":"Test","slug":"test"},
                "repositories":[],"applications":[],"tasks":[],"linked_source":False}
            raise AssertionError("Unexpected controller action: "+op)
        self.app=create_app({"TESTING":True,"SECRET_KEY":"test","SESSION_COOKIE_SECURE":False},transport=transport)
        self.client=self.app.test_client()
        self.client.get("")
        with self.client.session_transaction() as session:self.csrf=session["_csrf"]

    def test_config_and_admin_deep_links_are_narrow_and_side_effect_free(self):
        for route in ("/administration","/administration/ssh",
                      "/administration/updates",
                      f"/projects/{PROJECT}/configuration",
                      f"/projects/{PROJECT}/configuration/compose",
                      f"/projects/{PROJECT}/configuration/definitions",
                      f"/projects/{PROJECT}/configuration/webhooks",
                      f"/projects/{PROJECT}/configuration/name"):
            with self.subTest(route=route):self.assertEqual(self.client.get(route).status_code,200)
        for path in ("/administration/terminal",
                     f"/projects/{PROJECT}/configuration/shell",
                     f"/projects/{PROJECT}/configuration/compose/exec"):
            with self.subTest(path=path):self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.calls,[])

    def test_cas_contract_requires_exact_json_revision_and_csrf(self):
        url=f"/api/v2/projects/{PROJECT}/settings-review"
        self.assertEqual(self.client.get(url).get_json()["revision"],REV)
        self.calls.clear()
        payload={"applications":[],"tasks":[],"revision":REV}
        self.assertEqual(self.client.put(url,json=payload).status_code,403)
        self.assertEqual(self.calls,[])
        headers={"X-CSRF-Token":self.csrf}
        for invalid in (
            {"applications":[],"tasks":[]},
            {"applications":[],"tasks":[],"revision":"bad"},
            {"applications":[],"tasks":[],"revision":REV,"extra":1},
            {"applications":{},"tasks":[],"revision":REV},
        ):
            self.assertEqual(self.client.put(url,headers=headers,json=invalid).status_code,400)
        self.assertEqual(self.calls,[])
        resp=self.client.put(url,headers=headers,json=payload)
        self.assertEqual(resp.status_code,200)
        self.assertEqual(self.calls,[("settings_replace_checked",{
            "project_id":PROJECT,**payload,
        })])

    def test_admin_and_webhooks_preserve_exact_api_boundaries(self):
        headers={"X-CSRF-Token":self.csrf}
        base=f"/api/v2/projects/{PROJECT}/webhooks/github"
        self.assertEqual(self.client.put(base,json={
            "service":"web","port":8000,"destination":"/hooks"
        }).status_code,403)
        self.assertEqual(self.client.put(base,headers=headers,json={
            "service":"web","port":8000
        }).status_code,400)
        self.assertEqual(self.client.get("/api/v2/github/ssh").status_code,200)
        self.assertEqual(self.client.post("/api/v2/github/ssh/generate").status_code,403)
        self.assertEqual(self.client.post("/api/v2/devstack/update-now",json={}).status_code,403)
        self.assertEqual(self.calls,[("github_ssh_status",{})])

    def test_canonical_collection_review_is_deterministic(self):
        service=object.__new__(ConfigurationService)
        service.settings=lambda _project:{"tasks":[],"applications":[]}
        original=service.settings_review(PROJECT)["revision"]
        service.settings=lambda _project:{"applications":[],"tasks":[]}
        self.assertEqual(service.settings_review(PROJECT)["revision"],original)
        service.settings=lambda _project:{"applications":[{"name":"new","service":"api"}],"tasks":[]}
        self.assertNotEqual(service.settings_review(PROJECT)["revision"],original)

    def test_controller_rejects_stale_collection_revision_inside_lock(self):
        service=object.__new__(ConfigurationService)
        service.coordinator=SimpleNamespace(locked=lambda:nullcontext())
        service._editable=lambda _project:None
        service.registry=SimpleNamespace(get=lambda _project:True)
        service.settings_review=lambda _project:{"revision":"b"*64}
        with self.assertRaises(ConfigurationConflict):
            service.replace_settings(PROJECT, [], [], expected_revision=REV)

if __name__=="__main__":unittest.main()
