"""LLM diagnosis on Amazon Bedrock (Claude) grounded in the knowledge base.

The rule engine retrieves the top candidates (retrieval step); Claude reasons over
the incident plus those candidates and the full id catalog, and must answer with
a known pattern id or "unknown". Output is constrained to a JSON schema.
"""

from __future__ import annotations

import json
import os

from anthropic import AnthropicBedrockMantle, BetaFallbackState, BetaRefusalFallbackMiddleware

from .engine import Candidate, load_patterns

MODEL = os.environ.get("PIPELINE_DOCTOR_MODEL", "anthropic.claude-opus-5-5")
FALLBACK_MODEL = os.environ.get("PIPELINE_DOCTOR_FALLBACK_MODEL", "anthropic.claude-opus-4-8")
REGION = os.environ.get("AWS_REGION", "us-east-1")

SYSTEM = """You diagnose failures in AWS data pipelines (AWS Glue, Amazon MWAA / Apache Airflow,
Amazon Athena, AWS Lake Formation). You receive an incident (error text, logs, configuration)
and candidate root causes retrieved from a knowledge base by signature matching.

Pick the single most likely root cause from the catalog. Candidates are hints, not answers:
signature matching can rank a symptom above its cause, and plain-language reports may match
nothing. Prefer the cause over the symptom (for example a SIGKILL from memory pressure over
the zombie-task detection it triggers). Use configuration facts to separate look-alike errors.
If nothing in the catalog fits, answer "unknown". Do not invent resource names or settings
that are not in the incident."""


def _schema(ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "root_cause_id": {"type": "string", "enum": ids + ["unknown"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reasoning": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "next_checks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["root_cause_id", "confidence", "reasoning", "evidence", "next_checks"],
        "additionalProperties": False,
    }


def _catalog() -> str:
    return "\n".join(f"- {p.id} [{p.service}]: {p.title}" for p in load_patterns())


def _prompt(incident: dict, candidates: list[Candidate]) -> str:
    cands = "\n".join(
        f"- {c.pattern.id} (rule score {c.score:.1f}): {c.pattern.root_cause} Evidence: {'; '.join(c.evidence)}"
        for c in candidates
    ) or "- none matched"
    return (
        f"<incident>\n{json.dumps(incident, indent=2)}\n</incident>\n\n"
        f"<retrieved_candidates>\n{cands}\n</retrieved_candidates>\n\n"
        f"<catalog>\n{_catalog()}\n</catalog>"
    )


_client = None


def client() -> AnthropicBedrockMantle:
    global _client
    if _client is None:
        _client = AnthropicBedrockMantle(
            aws_region=REGION,
            middleware=[BetaRefusalFallbackMiddleware([{"model": FALLBACK_MODEL}])],
        )
    return _client


def diagnose(incident: dict, candidates: list[Candidate], effort: str = "medium") -> dict:
    """Return the model's structured diagnosis. Raises on API errors."""
    ids = [p.id for p in load_patterns()]
    with BetaFallbackState():
        resp = client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": _prompt(incident, candidates)}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": _schema(ids)}},
        )
    if resp.stop_reason == "refusal":
        return {"root_cause_id": "unknown", "confidence": "low", "reasoning": "model declined",
                "evidence": [], "next_checks": [], "usage": None}
    text = next(b.text for b in resp.content if b.type == "text")
    out = json.loads(text)
    out["usage"] = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return out
