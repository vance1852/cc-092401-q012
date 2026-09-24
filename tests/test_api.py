from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from robot_trials.api import JsonApplication
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TrialService(self.connection)
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def _prepare_two_batches(self) -> None:
        service = self.service
        service.create_user("operator", "操作员", "operator")
        service.create_user("stat", "统计负责人", "statistician")
        service.create_user("approver", "审批人", "approver")
        protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        service.register_robot("operator", "robot-a", "A 型", "厂商")
        service.register_build("operator", "build-a", "robot-a", "1.0", "a" * 64)
        service.register_build("operator", "build-b", "robot-a", "1.1", "b" * 64)
        service.publish_protocol("stat", protocol)
        for batch_id, build_id in (("batch-a", "build-a"), ("batch-b", "build-b")):
            service.create_batch("operator", batch_id, "demo-delivery-v1", 1, build_id)
            service.start_batch("operator", batch_id, 1)
            service.import_observations("operator", batch_id, f"key-{batch_id}", rows)
            service.seal_batch("stat", batch_id, 2)
            job = service.claim_job("worker", 30)
            analysis = service.complete_job("worker", job["job_id"], "stat")
            service.decide("approver", batch_id, analysis["analysis_id"], "approved", "通过")

    def test_comparison_routes(self) -> None:
        self._prepare_two_batches()
        payload = json.dumps({
            "baseline_batch_id": "batch-a",
            "candidate_batch_id": "batch-b",
            "rules": [
                {"metric": "completed", "type": "non_inferior", "margin": "0.6"},
                {"metric": "completion_seconds", "type": "non_inferior", "margin": "10"},
                {"metric": "interventions", "type": "non_inferior", "margin": "1.5"},
            ],
        }).encode()
        created = self.app.handle("POST", "/comparisons", {"X-Actor-Id": "stat"}, payload)
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["result"]["conclusion"], "established")
        replayed = self.app.handle("POST", "/comparisons", {"X-Actor-Id": "stat"}, payload)
        self.assertEqual(replayed.status, 200)
        self.assertEqual(replayed.body["comparison_id"], created.body["comparison_id"])
        comparison_id = created.body["comparison_id"]
        fetched = self.app.handle("GET", f"/comparisons/{comparison_id}", {"X-Actor-Id": "stat"})
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["spec"]["rules"][0]["type"], "non_inferior")
        report = self.app.handle("GET", "/batches/batch-a/report", {"X-Actor-Id": "stat"})
        self.assertEqual(report.status, 200)
        self.assertEqual(report.body["comparisons"][0]["role"], "baseline")
        forbidden = self.app.handle("POST", "/comparisons", {"X-Actor-Id": "operator"}, payload)
        self.assertEqual(forbidden.status, 403)


if __name__ == "__main__":
    unittest.main()
