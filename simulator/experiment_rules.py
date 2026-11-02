"""Public, frozen collaboration rules; contains no scene data or target truth."""

from copy import deepcopy
from math import hypot, isfinite

RULES = {
    "version": "authorized_capture_v1",
    "observation_period": 3,
    "position_report_period": 1,
    "information_max_age": 5,
    "authorization_ttl": 5,
    "authorization_distance": 5.0,
    "marker_radius": 5.0,
    "hold_seconds": 3,
    "hold_clock": "consecutive_completed_tick_endpoints",
    "support_required": False,
    "authorization_required": True,
    "decision_policy": "latest_positive_sample_and_reported_distance_no_prediction",
    "renewal_policy": "only_after_previous_grant_expires",
}


def frozen_rules(value=None):
    if value is not None and value != RULES:
        raise ValueError("experiment rules mismatch; rebuild all experiment components")
    return deepcopy(RULES)


def valid_point(point):
    return (isinstance(point, list) and len(point) == 2
            and all(type(x) in (int, float) and isfinite(x) for x in point))


def validate_authorization(grant, episode, roles):
    if not isinstance(grant, dict):
        raise ValueError("authorization must be an object")
    required = {"authorization_id", "episode_id", "leader_id", "marker_id", "observer_id",
                "observed_at", "target_position", "position_at", "marker_position",
                "issued_at", "expires_at", "estimated_distance", "rules_version"}
    if set(grant) != required:
        raise ValueError("invalid authorization fields")
    if (not isinstance(grant["authorization_id"], str) or not grant["authorization_id"]
            or grant["episode_id"] != episode or grant["rules_version"] != RULES["version"]
            or roles.get(grant["leader_id"]) != "leader"
            or roles.get(grant["marker_id"]) != "marker"
            or roles.get(grant["observer_id"]) != "observer"):
        raise ValueError("authorization identity/rules mismatch")
    for key in ("observed_at", "position_at", "issued_at", "expires_at"):
        if type(grant[key]) is not int or grant[key] < 0:
            raise ValueError("invalid authorization clock")
    issued = grant["issued_at"]
    if (grant["expires_at"] != issued + RULES["authorization_ttl"]
            or any(not 0 <= issued - grant[key] <= RULES["information_max_age"]
                   for key in ("observed_at", "position_at"))):
        raise ValueError("authorization has stale evidence or invalid validity interval")
    if not all(valid_point(grant[key]) for key in ("target_position", "marker_position")):
        raise ValueError("invalid authorization reported coordinates")
    estimate = hypot(*(a - b for a, b in zip(grant["target_position"], grant["marker_position"])))
    if (type(grant["estimated_distance"]) not in (int, float)
            or not isfinite(grant["estimated_distance"])
            or abs(estimate - grant["estimated_distance"]) > 1e-9
            or estimate > RULES["authorization_distance"] + 1e-9):
        raise ValueError("authorization does not satisfy the known-distance policy")
    return grant


def authorization_valid(grant, second):
    return grant["issued_at"] <= second < grant["expires_at"]
