"""Rule engine: score knowledge-base patterns against an incident.

An incident is a dict: {"service": "glue"|"mwaa"|"athena"|"lakeformation" (optional),
"error": str, "logs": str, "config": dict}. Signatures match text; checks look at
config so that generic errors (timeouts, AccessDenied) resolve to the right cause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

import yaml


@dataclass(frozen=True)
class Pattern:
    id: str
    service: str
    title: str
    signatures: tuple[re.Pattern, ...]
    root_cause: str
    fix: tuple[str, ...]
    doc: str
    checks: tuple[str, ...] = ()


@dataclass
class Candidate:
    pattern: Pattern
    score: float
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0


def _get(cfg: dict, path: str):
    for key in path.split("."):
        if not isinstance(cfg, dict) or key not in cfg:
            return None
        cfg = cfg[key]
    return cfg


def _no_s3_route(cfg):
    if _get(cfg, "connection") is None:
        return None
    s3, nat = _get(cfg, "vpc.s3_endpoint"), _get(cfg, "vpc.nat_gateway")
    return None if s3 is None and nat is None else not (s3 or nat)


def _no_internet_route(cfg):
    nat = _get(cfg, "vpc.nat_gateway")
    return None if nat is None else not nat


def _sg_missing_self_reference(cfg):
    v = _get(cfg, "security_group.self_referencing")
    return None if v is None else not v


def _workers_capped(cfg):
    mx, mn, running = (_get(cfg, f"mwaa.{k}") for k in ("max_workers", "min_workers", "running_workers"))
    if mx is None:
        return None
    return (mn is not None and mx <= mn) or (running is not None and running >= mx)


def _iam_allowed_principals_present(cfg):
    return _get(cfg, "lakeformation.iam_allowed_principals")


def _ram_invitation_pending(cfg):
    v = _get(cfg, "lakeformation.ram_invitation")
    return None if v is None else v.upper() == "PENDING"


# Each check returns True (supports the pattern), False (contradicts it) or None (no data).
CHECKS = {
    "no_s3_route": _no_s3_route,
    "no_internet_route": _no_internet_route,
    "sg_missing_self_reference": _sg_missing_self_reference,
    "workers_capped": _workers_capped,
    "iam_allowed_principals_present": _iam_allowed_principals_present,
    "ram_invitation_pending": _ram_invitation_pending,
}

CHECK_WEIGHT = 2.0
SERVICE_MISMATCH_FACTOR = 0.4


@lru_cache(maxsize=1)
def load_patterns() -> tuple[Pattern, ...]:
    raw = yaml.safe_load(resources.files("pipeline_doctor").joinpath("patterns.yaml").read_text())
    out = []
    for p in raw:
        unknown = set(p.get("checks", [])) - CHECKS.keys()
        if unknown:
            raise ValueError(f"{p['id']}: unknown checks {unknown}")
        out.append(Pattern(
            id=p["id"], service=p["service"], title=p["title"],
            signatures=tuple(re.compile(s, re.I) for s in p["signatures"]),
            root_cause=p["root_cause"], fix=tuple(p["fix"]), doc=p["doc"],
            checks=tuple(p.get("checks", [])),
        ))
    return tuple(out)


def rank(incident: dict, top_k: int = 5) -> list[Candidate]:
    """Return up to top_k candidate patterns, best first, with normalized confidence."""
    text = f"{incident.get('error', '')}\n{incident.get('logs', '')}"
    cfg = incident.get("config") or {}
    service = (incident.get("service") or "").lower()
    cands = []
    for p in load_patterns():
        evidence = [f"log matches /{s.pattern}/" for s in p.signatures if s.search(text)]
        if not evidence:
            continue
        score = float(len(evidence))
        for name in p.checks:
            result = CHECKS[name](cfg)
            if result is True:
                score += CHECK_WEIGHT
                evidence.append(f"config check {name} = true")
            elif result is False:
                score -= CHECK_WEIGHT
                evidence.append(f"config check {name} = false")
        # Root causes often sit in a neighbouring service (a Glue job failing on a
        # Lake Formation grant), so a service mismatch down-weights instead of filtering.
        if service and p.service != service:
            score *= SERVICE_MISMATCH_FACTOR
        if score > 0:
            cands.append(Candidate(p, score, evidence))
    cands.sort(key=lambda c: c.score, reverse=True)
    total = sum(c.score for c in cands) or 1.0
    for c in cands:
        c.confidence = round(c.score / total, 3)
    return cands[:top_k]
