import json
from pathlib import Path
from types import SimpleNamespace

import boto3
import yaml
from botocore.stub import Stubber

from pipeline_doctor import collectors, llm
from pipeline_doctor.cli import evaluate, run
from pipeline_doctor.engine import load_patterns, rank

SCENARIOS = Path(__file__).parent.parent / "evals" / "scenarios.yaml"


def test_knowledge_base_is_consistent():
    patterns = load_patterns()
    ids = [p.id for p in patterns]
    assert len(ids) == len(set(ids)) == 31
    assert all(p.fix and p.doc.startswith("https://docs.aws.amazon.com/") for p in patterns)
    covered = {s["expected"] for s in yaml.safe_load(SCENARIOS.read_text())}
    assert covered == set(ids), "every pattern needs at least one eval scenario"


def test_rules_accuracy_does_not_regress():
    report = evaluate(SCENARIOS, use_llm=False)
    assert report["easy"]["top1"] == report["easy"]["n"]
    assert report["overall"]["top1_pct"] >= 88.0


def test_config_breaks_tie_between_lookalike_timeouts():
    err = "Unable to execute HTTP request: Connect to b.s3.us-east-1.amazonaws.com:443 failed: Connection timed out"
    no_s3 = rank({"service": "glue", "error": err, "config": {
        "connection": {}, "vpc": {"s3_endpoint": False, "nat_gateway": False}, "security_group": {"self_referencing": True}}})
    assert no_s3[0].pattern.id == "glue_vpc_no_s3_endpoint"
    no_sg = rank({"service": "glue", "error": "Communications link failure. connect timed out", "config": {
        "connection": {}, "vpc": {"s3_endpoint": True}, "security_group": {"self_referencing": False}}})
    assert no_sg[0].pattern.id == "glue_jdbc_sg_self_ref"


def test_unknown_incident_returns_unknown():
    assert run({"error": "everything is fine"})["root_cause_id"] == "unknown"


def test_self_referencing_rule_detection():
    session = boto3.Session(region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    ec2 = session.client("ec2")
    resp = {"SecurityGroups": [{"GroupId": "sg-1", "IpPermissions": [
        {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": "sg-1"}]}]}]}
    with Stubber(ec2) as stub:
        stub.add_response("describe_security_groups", resp, {"GroupIds": ["sg-1"]})
        session.client = lambda *_a, **_k: ec2
        assert collectors._self_referencing(["sg-1"], session) is True


def test_llm_diagnose_parses_structured_output(monkeypatch):
    answer = {"root_cause_id": "athena_too_many_partitions", "confidence": "high",
              "reasoning": "one partition per day for a year exceeds 100", "evidence": [], "next_checks": []}
    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(answer))],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=150)))))
    monkeypatch.setattr(llm, "_client", fake)
    out = run({"service": "athena", "error": "CTAS writing 365 daily partitions fails"}, use_llm=True)
    assert out["root_cause_id"] == "athena_too_many_partitions"
    assert out["engine"] == "rules+llm" and out["fix"]
