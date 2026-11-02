"""Fixed role quotas and distance ordering for the t=0 initialization.

This is a deterministic candidate registry, not an LLM discovery algorithm.
Only the initial observation is used; the target's future route is not used.
"""

from math import isfinite

from simulator.core import AgentState, EPS, distance


ROLE_ORDER = ("observer", "leader", "marker")
FIVE_ROLE_COUNTS = {"observer": 2, "leader": 1, "marker": 2}


def select_fixed_team(
    candidates: list[AgentState], target_position, observation_radius: float,
    radius: float | None = None, role_counts: dict | None = None,
) -> tuple[list[AgentState], dict]:
    quotas = dict(FIVE_ROLE_COUNTS if role_counts is None else role_counts)
    if (set(quotas) != set(ROLE_ORDER)
            or any(type(n) is not int or n < 1 for n in quotas.values())
            or quotas["leader"] != 1):
        raise ValueError("selection needs positive role quotas and exactly one leader")
    if radius is not None and (isinstance(radius, bool) or not isinstance(radius, (int, float))
                               or not isfinite(radius) or radius <= 0):
        raise ValueError("selection radius must be null (whole map) or positive and finite")
    if len({a.agent_id for a in candidates}) != len(candidates):
        raise ValueError("candidate IDs must be unique")
    distances = {a.agent_id: distance(a.pos, target_position) for a in candidates}
    eligible = [a for a in candidates if radius is None or distances[a.agent_id] <= radius + EPS]
    selected = []
    for role in ROLE_ORDER:
        ranked = sorted(
            (a for a in eligible if a.role == role),
            key=lambda a: (distances[a.agent_id], a.agent_id),
        )
        if len(ranked) < quotas[role]:
            raise ValueError(f"not enough {role} candidates within {radius} m: need {quotas[role]}, got {len(ranked)}")
        selected.extend(ranked[:quotas[role]])
    observers = [a for a in selected if a.role == "observer"]
    discoverer = observers[0]
    if distances[discoverer.agent_id] > observation_radius + EPS:
        raise ValueError("no selected observer can discover the target at t=0")
    selected_ids = [a.agent_id for a in selected]
    record = {
        "rule": "nearest_per_role_then_agent_id",
        "radius": radius, "role_counts": quotas,
        "candidate_count": len(candidates), "team_size": len(selected),
        "initial_report": {"observer_id": discoverer.agent_id, "observed_at": 0,
                           "target_position": list(target_position)},
        "leader_id": next(a.agent_id for a in selected if a.role == "leader"),
        "selected_ids": selected_ids,
        "candidates": [
            {"id": a.agent_id, "carrier_type": a.carrier_type, "role": a.role,
             "position": list(a.pos), "distance": distances[a.agent_id],
             "selected": a.agent_id in selected_ids,
             "reason": ("selected" if a.agent_id in selected_ids else
                        "outside_radius" if radius is not None and distances[a.agent_id] > radius + EPS
                        else "role_quota_filled")}
            for a in sorted(candidates, key=lambda a: a.agent_id)
        ],
    }
    return selected, record
