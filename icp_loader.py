"""Canonical ICP loader (Python).

The ONE place Python consumers read ICP rules. Do not re-encode rules elsewhere.
Reads icp_rules.yaml (the single source of truth) and exposes typed lookups.

Follows the repo's safety-critical style: bounded work, validated params,
assertions, no dynamic surprises.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

try:
    import yaml  # PyYAML
except ImportError as exc:  # pragma: no cover - environment guard
    raise RuntimeError("icp.loader requires PyYAML (pip install pyyaml)") from exc

_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icp_rules.yaml")

# Ordered seniority buckets, most senior first. Used to classify a raw title.
_SENIORITY_KEYS = ("vp_plus", "director", "manager", "ic")


@lru_cache(maxsize=1)
def rules() -> dict:
    """Load and cache the canonical rules. Asserts the file is well-formed."""
    assert os.path.exists(_RULES_PATH), f"missing rules file: {_RULES_PATH}"
    with open(_RULES_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), "icp_rules.yaml did not parse to a mapping"
    for key in ("segments", "size_bands", "seniority_x_size", "roles", "icp_version"):
        assert key in data, f"icp_rules.yaml missing required key: {key}"
    return data


def version() -> str:
    """Return the ICP version string."""
    return str(rules()["icp_version"])


def segment_class(segment: str) -> str:
    """Map a normalized segment token to 'icp' | 'mid' | 'deny' | 'unknown'."""
    assert isinstance(segment, str) and segment, "segment must be a non-empty str"
    seg = segment.strip().lower()
    segs = rules()["segments"]
    for cls in ("icp", "mid", "deny"):
        if seg in segs.get(cls, []):
            return cls
    return "unknown"


def size_band(employees: Optional[int]) -> str:
    """Return the band name for a headcount, or 'unknown' when None/negative."""
    if employees is None or employees < 0:
        return "unknown"
    for band in rules()["size_bands"]:            # bounded: fixed-size band list
        hi = band.get("max")
        if employees >= band.get("min", 0) and (hi is None or employees <= hi):
            return band["name"]
    return "unknown"


def role_allowed(role: str) -> bool:
    """True iff the role/function is on the allow-list (deny wins over allow)."""
    assert isinstance(role, str), "role must be a str"
    r = role.strip().lower()
    roles = rules()["roles"]
    if r in roles.get("deny", []):
        return False
    return r in roles.get("allow", [])


def tier(seniority: str, employees: Optional[int]) -> str:
    """Resolve outreach tier: 'priority' | 'warm' | 'okay' | 'skip'.

    seniority must be one of _SENIORITY_KEYS. Applies the seniority x size
    matrix and the sub-50 floor.
    """
    assert seniority in _SENIORITY_KEYS, f"unknown seniority: {seniority}"
    band = size_band(employees)
    if band == "floor":
        # Under-50: hard floor -> nobody is priority here.
        return "okay"
    matrix = rules()["seniority_x_size"].get(seniority, {})
    return matrix.get(band, matrix.get("default", "okay"))


def is_vp_plus(seniority: str) -> bool:
    """Convenience gate used by meetingbot VP+ escalation."""
    return seniority == "vp_plus"


if __name__ == "__main__":  # tiny self-check
    print(f"ICP version: {version()}")
    print(f"reinsurance_broker -> {segment_class('reinsurance_broker')}")
    print(f"reinsurer -> {segment_class('reinsurer')}")
    print(f"director @ 3000 -> {tier('director', 3000)}")
    print(f"vp_plus @ 120 -> {tier('vp_plus', 120)}")
    print(f"innovation allowed -> {role_allowed('innovation')}")
