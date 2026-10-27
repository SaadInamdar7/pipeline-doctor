"""Build incidents from live AWS resources. Read-only API calls only."""

from __future__ import annotations

import boto3


def _vpc_facts(subnet_ids: list[str], session: boto3.Session) -> dict:
    """S3 gateway endpoint / NAT presence for the route tables of the given subnets."""
    ec2 = session.client("ec2")
    tables = ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": subnet_ids}])["RouteTables"]
    if not tables:  # subnets fall back to the VPC main route table
        vpc = ec2.describe_subnets(SubnetIds=subnet_ids[:1])["Subnets"][0]["VpcId"]
        tables = ec2.describe_route_tables(Filters=[
            {"Name": "vpc-id", "Values": [vpc]}, {"Name": "association.main", "Values": ["true"]}])["RouteTables"]
    routes = [r for t in tables for r in t["Routes"]]
    s3_prefix_lists = {pl["PrefixListId"] for pl in ec2.describe_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": [f"com.amazonaws.{session.region_name}.s3"]}])["PrefixLists"]}
    return {
        "s3_endpoint": any(r.get("DestinationPrefixListId") in s3_prefix_lists and r.get("GatewayId", "").startswith("vpce-") for r in routes),
        "nat_gateway": any(r.get("NatGatewayId") for r in routes),
    }


def _self_referencing(sg_ids: list[str], session: boto3.Session) -> bool:
    groups = session.client("ec2").describe_security_groups(GroupIds=sg_ids)["SecurityGroups"]
    for g in groups:
        for perm in g["IpPermissions"]:
            all_tcp = perm.get("IpProtocol") == "-1" or (perm.get("FromPort"), perm.get("ToPort")) == (0, 65535)
            if all_tcp and any(p["GroupId"] in sg_ids for p in perm.get("UserIdGroupPairs", [])):
                return True
    return False


def glue_job_run(job_name: str, run_id: str, session: boto3.Session | None = None, log_lines: int = 200) -> dict:
    session = session or boto3.Session()
    glue = session.client("glue")
    run = glue.get_job_run(JobName=job_name, RunId=run_id)["JobRun"]
    job = glue.get_job(JobName=job_name)["Job"]
    config: dict = {"glue": {"version": job.get("GlueVersion"), "worker_type": run.get("WorkerType"),
                             "workers": run.get("NumberOfWorkers")}}
    conns = job.get("Connections", {}).get("Connections", [])
    if conns:
        conn = glue.get_connection(Name=conns[0], HidePassword=True)["Connection"]
        req = conn.get("PhysicalConnectionRequirements", {})
        config["connection"] = {"type": conn.get("ConnectionType")}
        if req.get("SubnetId"):
            config["vpc"] = _vpc_facts([req["SubnetId"]], session)
        if req.get("SecurityGroupIdList"):
            config["security_group"] = {"self_referencing": _self_referencing(req["SecurityGroupIdList"], session)}
    logs_client = session.client("logs")
    logs = ""
    try:
        events = logs_client.get_log_events(
            logGroupName="/aws-glue/jobs/error", logStreamName=run_id, limit=log_lines, startFromHead=False)["events"]
        logs = "\n".join(e["message"] for e in events)
    except logs_client.exceptions.ResourceNotFoundException:
        pass
    return {"service": "glue", "error": run.get("ErrorMessage", ""), "logs": logs, "config": config}


def mwaa_environment(name: str, error: str, session: boto3.Session | None = None) -> dict:
    session = session or boto3.Session()
    env = session.client("mwaa").get_environment(Name=name)["Environment"]
    config = {
        "mwaa": {"min_workers": env.get("MinWorkers"), "max_workers": env.get("MaxWorkers"),
                 "environment_class": env.get("EnvironmentClass"), "airflow_version": env.get("AirflowVersion")},
        "vpc": _vpc_facts(env["NetworkConfiguration"]["SubnetIds"], session),
    }
    return {"service": "mwaa", "error": error, "logs": "", "config": config}


def athena_query(query_id: str, session: boto3.Session | None = None) -> dict:
    session = session or boto3.Session()
    q = session.client("athena").get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
    status = q["Status"]
    error = status.get("AthenaError", {}).get("ErrorMessage") or status.get("StateChangeReason", "")
    return {"service": "athena", "error": error, "logs": "",
            "config": {"athena": {"engine": q.get("EngineVersion", {}).get("EffectiveEngineVersion"),
                                  "workgroup": q.get("WorkGroup")}}}
