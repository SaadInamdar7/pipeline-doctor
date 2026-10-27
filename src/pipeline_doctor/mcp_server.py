"""MCP server (stdio) exposing pipeline-doctor as agent tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import collectors
from .cli import run
from .engine import load_patterns

mcp = MCPServer("pipeline-doctor")


@mcp.tool()
def diagnose_incident(error: str, logs: str = "", service: str = "", config: dict | None = None, use_llm: bool = False) -> dict:
    """Diagnose a data-pipeline failure from error text, logs, and optional config facts.

    service: glue | mwaa | athena | lakeformation (optional). config keys the engine reads:
    connection, vpc.s3_endpoint, vpc.nat_gateway, security_group.self_referencing,
    mwaa.min_workers/max_workers/running_workers, lakeformation.iam_allowed_principals,
    lakeformation.ram_invitation.
    """
    return run({"service": service, "error": error, "logs": logs, "config": config or {}}, use_llm)


@mcp.tool()
def diagnose_glue_job_run(job_name: str, run_id: str, use_llm: bool = False) -> dict:
    """Fetch a Glue job run, its connection's VPC/security-group facts and error logs (read-only), then diagnose."""
    return run(collectors.glue_job_run(job_name, run_id), use_llm)


@mcp.tool()
def diagnose_mwaa_environment(name: str, error: str, use_llm: bool = False) -> dict:
    """Fetch MWAA worker and network settings (read-only) and diagnose the given error or symptom."""
    return run(collectors.mwaa_environment(name, error), use_llm)


@mcp.tool()
def diagnose_athena_query(query_execution_id: str, use_llm: bool = False) -> dict:
    """Fetch an Athena query execution's failure reason (read-only) and diagnose it."""
    return run(collectors.athena_query(query_execution_id), use_llm)


@mcp.tool()
def list_failure_patterns(service: str = "") -> list[dict]:
    """List the knowledge-base failure patterns, optionally for one service."""
    return [{"id": p.id, "service": p.service, "title": p.title}
            for p in load_patterns() if not service or p.service == service]


def main() -> None:
    mcp.run("stdio")  # local stdio only; no network listener


if __name__ == "__main__":
    main()
