"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import load_json
from .service import TrialService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    protocol = load_json(fixtures / "demo_protocol.json")
    observation_rows = [
        json.loads(line)
        for line in (fixtures / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="robot-trials-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        try:
            service = TrialService(connection)
            service.create_user("operator-1", "测试操作员", "operator")
            service.create_user("stat-1", "统计负责人", "statistician")
            service.create_user("approver-1", "准入审批人", "approver")
            service.create_user("auditor-1", "审计人员", "auditor")
            service.register_robot("operator-1", "robot-a", "A 型人形机器人", "示例厂商")
            service.register_build("operator-1", "build-a1", "robot-a", "1.0.0", "a" * 64)
            service.publish_protocol("stat-1", protocol)
            service.create_batch("operator-1", "batch-demo", protocol["protocol_id"], protocol["version"], "build-a1")
            service.start_batch("operator-1", "batch-demo", 1)
            imported = service.import_observations(
                "operator-1", "batch-demo", "demo-import-1", observation_rows
            )
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("worker-1", lease_seconds=60)
            if job is None:
                raise RuntimeError("未能领取分析任务")
            analysis = service.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )
            service.register_build("operator-1", "build-a2", "robot-a", "1.1.0", "c" * 64)
            service.create_batch(
                "operator-1", "batch-demo-2", protocol["protocol_id"], protocol["version"], "build-a2"
            )
            service.start_batch("operator-1", "batch-demo-2", 1)
            service.import_observations("operator-1", "batch-demo-2", "demo-import-2", observation_rows)
            service.seal_batch("stat-1", "batch-demo-2", 2)
            candidate_job = service.claim_job("worker-1", lease_seconds=60)
            if candidate_job is None:
                raise RuntimeError("未能领取候选批次分析任务")
            candidate_analysis = service.complete_job("worker-1", candidate_job["job_id"], "stat-1")
            candidate_decision = (
                "approved" if candidate_analysis["result"]["conclusion"] == "pass" else "rejected"
            )
            service.decide(
                "approver-1", "batch-demo-2", candidate_analysis["analysis_id"],
                candidate_decision, "离线验收决定",
            )
            comparison_rules = [
                {"metric": "completed", "type": "non_inferior", "margin": "0.6"},
                {"metric": "completion_seconds", "type": "non_inferior", "margin": "10"},
                {"metric": "interventions", "type": "non_inferior", "margin": "1.5"},
            ]
            comparison = service.create_comparison(
                "stat-1", "batch-demo", "batch-demo-2", comparison_rules
            )
            replayed = service.create_comparison(
                "stat-1", "batch-demo", "batch-demo-2", comparison_rules
            )
            if not replayed["replayed"] or replayed["comparison_id"] != comparison["comparison_id"]:
                raise RuntimeError("对照分析幂等重算失败")
            report = service.report("auditor-1", "batch-demo")
            if len(report["comparisons"]) != 1:
                raise RuntimeError("对照分析未进入批次报告")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "protocol": f"{protocol['protocol_id']}@{protocol['version']}",
        "observation_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "comparison_id": comparison["comparison_id"],
        "comparison_conclusion": comparison["result"]["conclusion"],
        "event_count": len(report["events"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行试验数据基础工具的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
