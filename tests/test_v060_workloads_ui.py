"""I5: staged mobile workload views use existing controller/IPC boundaries."""
import unittest
from uuid import uuid4
from devstack.api.http import create_app

PROJECT=str(uuid4())
JOB=str(uuid4())
CONTAINER="a"*64

class WorkloadsPreviewTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        def transport(op, **fields):
            self.calls.append((op,fields))
            responses={
                "services":{"active":True,"services":[]},
                "private_ports":{"active":True,"tailscale_available":False,"ports":[],"applications":[]},
                "jobs":{"jobs":[]},
                "job":{"id":JOB,"project_id":PROJECT,"status":"running","cancel_policy":"independent"},
                "service_logs":{"container_id":CONTAINER,"service":"web","text":"ok","tail":50,"truncated":False},
                "job_logs":{"text":"ok","offset":0,"next_offset":2,"complete":True},
                "task":{"job_id":JOB},
                "compose":{"job_id":JOB},
                "adhoc":{"job_id":JOB},
                "job_cancel":{"id":JOB,"project_id":PROJECT,"status":"cancelled"},
            }
            return responses[op]
        self.app=create_app({"TESTING":True,"SECRET_KEY":"test",
                             "SESSION_COOKIE_SECURE":False},transport=transport)
        self.client=self.app.test_client()
        self.client.get("")
        with self.client.session_transaction() as session:self.csrf=session["_csrf"]
    def test_narrow_deep_links_never_touch_controller(self):
        for path in (
            f"/projects/{PROJECT}/services",
            f"/projects/{PROJECT}/services/{CONTAINER}",
            f"/projects/{PROJECT}/applications",
            f"/projects/{PROJECT}/jobs",
            f"/projects/{PROJECT}/jobs/{JOB}",
            f"/projects/{PROJECT}/jobs/{JOB}/logs",
            f"/projects/{PROJECT}/jobs/run/task",
            f"/projects/{PROJECT}/jobs/run/adhoc",
            f"/projects/{PROJECT}/jobs/run/compose",
        ):
            with self.subTest(path=path):self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.calls,[])
        for path in (
            f"/projects/{PROJECT}/services/abcdef",
            f"/projects/{PROJECT}/services/{CONTAINER}/exec",
            f"/projects/{PROJECT}/jobs/run/shell",
            f"/projects/{PROJECT}/jobs/{JOB}/interactive",
        ):
            with self.subTest(path=path):self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.calls,[])
    def test_reads_are_scoped_and_bounded(self):
        self.assertEqual(self.client.get(f"/api/v2/projects/{PROJECT}/services").status_code,200)
        self.assertEqual(self.client.get(f"/api/v2/projects/{PROJECT}/private-ports").status_code,200)
        self.assertEqual(self.client.get(f"/api/v2/projects/{PROJECT}/jobs").status_code,200)
        self.assertEqual(self.client.get(f"/api/v2/projects/{PROJECT}/services/{CONTAINER}/logs?tail=50").status_code,200)
        self.assertEqual(self.client.get(f"/api/v2/projects/{PROJECT}/services/{CONTAINER}/logs?tail=201").status_code,400)
        self.assertEqual(self.client.get(f"/api/v2/projects/{PROJECT}/jobs/{JOB}/logs?offset=2097153").status_code,400)
        self.assertEqual(self.calls,[
            ("services",{"project_id":PROJECT}),("private_ports",{"project_id":PROJECT}),
            ("jobs",{"project_id":PROJECT}),
            ("service_logs",{"project_id":PROJECT,"container_id":CONTAINER,"tail":50}),
        ])
    def test_job_mutations_require_csrf_and_preserve_narrow_fields(self):
        task=f"/api/v2/projects/{PROJECT}/tasks/demo/run"
        self.assertEqual(self.client.post(task).status_code,403)
        self.assertEqual(self.calls,[])
        headers={"X-CSRF-Token":self.csrf}
        self.assertEqual(self.client.post(task,headers=headers).status_code,202)
        self.assertEqual(self.calls[-1],("task",{"project_id":PROJECT,"name":"demo"}))
        self.assertEqual(self.client.post(f"/api/v2/projects/{PROJECT}/compose/build",headers=headers).status_code,202)
        self.assertEqual(self.calls[-1],("compose",{"project_id":PROJECT,"action":"build"}))
        adhoc={"kind":"run","service":"web","image":"","network":"none","command":["python","-V"]}
        self.assertEqual(self.client.post(f"/api/v2/projects/{PROJECT}/adhoc",headers=headers,json=adhoc).status_code,202)
        self.assertEqual(self.calls[-1],("adhoc",{"project_id":PROJECT,**adhoc}))
        self.assertEqual(self.client.post(f"/api/v2/projects/{PROJECT}/adhoc",headers=headers,
            json={"kind":"run","service":"web"}).status_code,400)
        self.assertEqual(self.client.post(f"/api/v2/projects/{PROJECT}/jobs/{JOB}/cancel",headers=headers).status_code,200)
        self.assertEqual(self.calls[-1],("job_cancel",{"project_id":PROJECT,"id":JOB}))

if __name__=="__main__":unittest.main()
