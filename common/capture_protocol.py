"""Capture protocol carried in existing text fields; no simulator truth access."""

import json
from math import isfinite
import re

ROUND_PREFIX = "IOA_CAPTURE_ROUND_V2\n"
TASK_PREFIX = "IOA_CAPTURE_TASK_V2\n"
BATCH_PREFIX = "IOA_CAPTURE_BATCH_V2\n"
RECEIPT_HEADER = "【执行器记录的真实工具回执】以下 JSON 由执行器生成，模型文字不能替代或补充回执。\n"
ROUND_KEY = re.compile(r"[0-9a-f]{32}:[0-9]{1,3}\Z")
ACTION_TOOLS = {"sim_observe", "sim_move_to", "sim_try_tag", "sim_self_state"}


def encode_round_goal(instructions, episode, second, period, members):
    return ROUND_PREFIX + json.dumps({
        "round_id": f"{episode}:{second}", "observation_period": period,
        "members": [{"id": m["id"], "role": m["role"]} for m in members],
        "instructions": instructions,
    }, ensure_ascii=False)


def decode_round_goal(goal, round_id):
    if not goal.startswith(ROUND_PREFIX):
        return None  # Legacy non-state-machine callers remain distinguishable.
    spec = json.loads(goal[len(ROUND_PREFIX):])
    if spec.get("round_id") != round_id or not ROUND_KEY.fullmatch(round_id):
        raise ValueError("capture round specification has the wrong round")
    if type(spec.get("observation_period")) is not int or spec["observation_period"] < 1:
        raise ValueError("invalid capture observation period")
    members = spec.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("capture round needs members")
    roles = {}
    for row in members:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                or row["id"] in roles or row.get("role") not in {"observer", "leader", "marker"}):
            raise ValueError("invalid capture member")
        roles[row["id"]] = row["role"]
    if (list(roles.values()).count("leader") != 1
            or "observer" not in roles.values() or "marker" not in roles.values()):
        raise ValueError("capture round needs observers, one leader and markers")
    return spec


def finite_coordinate(value):
    return type(value) in (int, float) and isfinite(value)


def validate_contract(contract, round_id, agent_name):
    if (not isinstance(contract, dict) or contract.get("round_id") != round_id
            or contract.get("agent_id") != agent_name or not ROUND_KEY.fullmatch(round_id)):
        raise ValueError("capture contract belongs to another agent or round")
    if (not isinstance(contract.get("id"), str) or not contract["id"]
            or contract.get("tool") not in ACTION_TOOLS):
        raise ValueError("invalid capture action contract")
    args = contract.get("arguments")
    if not isinstance(args, dict):
        raise ValueError("capture contract needs arguments")
    if contract["tool"] == "sim_move_to":
        if set(args) != {"x", "y"} or not all(finite_coordinate(v) for v in args.values()):
            raise ValueError("waypoint contract needs finite x and y")
        observed_at = contract.get("observed_at")
        if type(observed_at) is not int or not 0 <= observed_at <= int(round_id.split(":")[1]):
            raise ValueError("waypoint contract needs a valid observed_at")
    elif args:
        raise ValueError("observation and tag contracts take no model arguments")
    if "authorization_id" in contract and (contract["tool"] != "sim_try_tag"
            or not isinstance(contract["authorization_id"], str) or not contract["authorization_id"]):
        raise ValueError("invalid tag authorization reference")
    return contract


def decode_task(text, round_id, agent_name):
    if not text.startswith(TASK_PREFIX):
        return None
    return validate_contract(json.loads(text[len(TASK_PREFIX):]), round_id, agent_name)


def check_contract_receipt(contract, tool, result):
    episode, second = contract["round_id"].split(":")
    if (not isinstance(result, dict) or result.get("error") or result.get("episode_id") != episode
            or type(result.get("time")) is not int or result["time"] != int(second)):
        raise ValueError("capture receipt does not match its contract round")
    if tool == "sim_self_state":
        point = result.get("position")
        if (result.get("agent_id") != contract["agent_id"] or not isinstance(point, list)
                or len(point) != 2 or not all(finite_coordinate(v) for v in point)):
            raise ValueError("self state belongs to another agent")
        return
    if tool != contract["tool"]:
        raise ValueError(f"contract requires {contract['tool']}, got {tool}")
    if tool in {"sim_move_to", "sim_try_tag"} and type(result.get("accepted")) is not bool:
        raise ValueError("action receipt needs boolean accepted")
    if tool == "sim_try_tag" and contract.get("authorization_id") is not None:
        if (result.get("authorization_id") != contract["authorization_id"]
                or result.get("authorization_valid") is not True):
            raise ValueError("tag receipt has no verified valid authorization")
    if tool == "sim_move_to":
        if not result["accepted"] or result.get("waypoint") != [contract["arguments"]["x"], contract["arguments"]["y"]]:
            raise ValueError("waypoint receipt differs from assigned coordinates")
    if tool == "sim_observe":
        point = result.get("target_position")
        if (result.get("observer_id") != contract["agent_id"] or "target_position" not in result
                or type(result.get("observed_at")) is not int
                or not 0 <= result["observed_at"] <= int(second)
                or point is not None and (not isinstance(point, list) or len(point) != 2
                                          or not all(finite_coordinate(v) for v in point))):
            raise ValueError("invalid observation receipt")


def decode_receipt_report(text, contract):
    if not isinstance(text, str) or not text.startswith(RECEIPT_HEADER):
        raise ValueError("capture task returned no executor receipt envelope")
    payload, _ = json.JSONDecoder().raw_decode(text[len(RECEIPT_HEADER):])
    if (payload.get("protocol") != "ioa-capture-tool-result-v1" or payload.get("contract") != contract
            or payload.get("agent_name") != contract["agent_id"]
            or payload.get("sim_round_id") != contract["round_id"] or not payload.get("uid")):
        raise ValueError("capture result contract/identity mismatch; rebuild both client and tool images")
    receipts = payload.get("tool_receipts")
    if not isinstance(receipts, list):
        raise ValueError("missing executor receipts")
    actions = []
    for row in receipts:
        check_contract_receipt(contract, row["tool"], row["result"])
        if row["tool"] == contract["tool"]:
            actions.append(row["result"])
    if len(actions) != 1:
        raise ValueError("capture task must return exactly one assigned action receipt")
    return actions[0]
