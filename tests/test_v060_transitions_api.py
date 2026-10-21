"""v0.6: browser access to the controller-owned durable transition journal."""
import unittest
from uuid import uuid4
from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
A = str(uuid4())
B = str(uuid4())
KEY = "1234567890abcdef1234567890abcdef"
OPERATION = str(uuid4())

class TransitionHttpTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.receipt = {
            "id": OPERATION, "request_key": KEY, "action": "switch",
            "project_id": B, "status": "queued",
            "result": None, "error_code": "",
            "started_ns": 1, "finished_ns": None,
        }
        def transport(op, **fields):
            self.calls.append((op, fields))
            if op == "transition_submit":
                return {**self.receipt, **{
                    "request_key": fields["request_key"],
                    "project_id": fields["project_id"],
                    "action": fields["action"],
                }}
            if op == "transition_status":
                if fields["id"] == OPERATION:
                    return self.receipt
                raise ControllerRejected("TransitionNotFound")
            if op == "transition_list":
                return {"operations": [self.receipt]}
            if op == "status":
                return {"phase": "active", "project_id": A}
            raise AssertionError(f"Unexpected controller operation: {op}")
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test",
                               "SESSION_COOKIE_SECURE": False}, transport=transport)
        self.client = self.app.test_client()
        self.client.get("/__v06")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    def post(self, action="switch", project_id=B, key=KEY, csrf=True):
        headers = {"Idempotency-Key": key}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.post("/api/v2/runtime/transitions",
                                json={"action": action, "project_id": project_id},
                                headers=headers)

    def test_valid_json_submission_is_202_and_exact_controller_contract(self):
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["operation"]["id"], OPERATION)
        self.assertEqual(response.headers["Location"],
                         "/api/v2/runtime/transitions/" + OPERATION)
        self.assertEqual(self.calls, [("transition_submit", {
            "action": "switch", "project_id": B, "request_key": KEY,
        })])
        another = self.post()
        self.assertEqual(another.status_code, 202)
        self.assertEqual(self.calls[1], self.calls[0])

    def test_reads_are_narrow_and_never_mutate_runtime(self):
        self.assertEqual(self.client.get("/api/v2/runtime/transitions").get_json(),
                         {"operations": [self.receipt]})
        self.assertEqual(self.client.get("/api/v2/runtime/transitions/" + OPERATION).get_json(),
                         self.receipt)
        self.assertEqual(self.client.get("/api/v2/runtime/transitions/" + str(uuid4())).status_code, 404)
        self.assertEqual(self.client.get("/api/v2/runtime/transitions/incorrect").status_code, 404)
        self.assertEqual([op for op, _ in self.calls],
                         ["transition_list", "transition_status", "transition_status"])

    def test_csrf_strict_shape_and_idempotency(self):
        for response in (
            self.post(csrf=False),
            self.post(key="not-a-key"),
            self.post(key="A"*32),
            self.post(action="run-shell"),
            self.post(action="activate",project_id=None),
            self.post(action="reconcile",project_id=B),
            self.post(action="deactivate",project_id=A),
            self.post(project_id="invalid-uuid"),
            self.client.post("/api/v2/runtime/transitions",headers={
                "X-CSRF-Token": self.csrf, "Idempotency-Key": KEY,
            },json={"action":"switch","project_id":B,"unexpected":True}),
            self.client.post("/api/v2/runtime/transitions",headers={
                "X-CSRF-Token": self.csrf, "Idempotency-Key":KEY,
            },data="hello", content_type="text/plain"),
        ):
            self.assertIn(response.status_code, {400,403,415})
        self.assertEqual(self.calls, [])
        for action in ("deactivate", "reconcile"):
            response=self.post(action=action, project_id=None)
            self.assertEqual(response.status_code, 202)

    def test_unknown_outcome_not_retried_automatically_by_get(self):
        self.assertEqual(self.client.get("/__v06/runtime/operations").status_code, 200)
        self.assertEqual(self.client.get("/__v06/runtime/operations/" + OPERATION).status_code, 200)
        self.assertEqual(self.client.get("/__v06/runtime/reconcile").status_code, 200)
        self.assertEqual(self.client.get("/__v06/runtime/deactivate").status_code, 200)
        self.assertEqual(self.client.get("/__v06/projects/" + B + "/runtime/switch").status_code, 200)
        self.assertEqual(self.calls, [])

if __name__ == "__main__":
    unittest.main()
