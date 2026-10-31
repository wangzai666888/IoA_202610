"""Per-session capture phases driven by delivered executor receipts, not prose."""

from copy import deepcopy
import json
from uuid import uuid4

from common.capture_protocol import (
    BATCH_PREFIX, decode_round_goal, validate_contract, decode_receipt_report, finite_coordinate,
)


def begin_capture_round(goal, round_id, previous=None):
    spec = decode_round_goal(goal, round_id)
    if spec is None:
        return None
    episode, second = round_id.split(":")
    continuing = previous is not None and previous["episode"] == episode
    if continuing and (int(second) != previous["time"] + 1 or not ready_to_close(previous)):
        raise ValueError("capture phase state cannot advance before all required receipts")
    roles = {m["id"]: m["role"] for m in spec["members"]}
    if continuing and (roles != previous["roles"] or spec["observation_period"] != previous["period"]):
        raise ValueError("capture members/observation period changed during episode")
    return {
        "round_id": round_id, "episode": episode, "time": int(second),
        "period": spec["observation_period"], "roles": roles,
        "observed": [], "moved": [], "tagged": [], "pending": {}, "completed": {}, "batches": {}, "started": [],
        "waypoints_decided": False, "captured": False, "errors": [],
        "observations": deepcopy(previous["observations"]) if continuing else {},
        "waypoints": deepcopy(previous["waypoints"]) if continuing else {},
    }


def role_members(state, role):
    return [name for name, value in state["roles"].items() if value == role]


def missing_observers(state):
    if state["time"] % state["period"]:
        return []
    return [name for name in role_members(state, "observer") if name not in state["observed"]]


def ready_to_close(state):
    return (not state["pending"] and not state["errors"] and not missing_observers(state)
            and (state["time"] != 0 or set(state["moved"]) == set(state["roles"]))
            and (state["captured"] or set(state["tagged"]) == set(role_members(state, "marker"))))


def next_phase(state):
    if state["errors"]:
        raise RuntimeError("capture task failed: " + "; ".join(state["errors"]))
    if state["pending"]:
        return "waiting"
    if missing_observers(state):
        return "observe"
    if not state["waypoints_decided"]:
        return "waypoint"
    return "complete" if ready_to_close(state) else "tag"


def make_batch(state, phase, actions):
    batch_id = uuid4().hex
    contracts = [{**a, "round_id": state["round_id"], "id": batch_id + ":" + a["agent_id"]}
                 for a in actions]
    return {"id": batch_id, "round_id": state["round_id"], "phase": phase, "tasks": contracts}


def decode_batch(content, sender):
    prefix = f"[{sender}]: "
    if content.startswith(prefix):
        content = content[len(prefix):]
    if not content.startswith(BATCH_PREFIX):
        raise ValueError("capture stage requires a structured task batch; mixed prose tasks are not executable")
    return json.loads(content[len(BATCH_PREFIX):])


def accept_batch(state, batch, sender, receivers):
    if sender not in role_members(state, "leader") or batch.get("round_id") != state["round_id"]:
        raise ValueError("capture batch has wrong leader or round")
    key = batch.get("id")
    if not isinstance(key, str) or not key:
        raise ValueError("capture batch needs an ID")
    if key in state["batches"]:
        if batch != state["batches"][key]:
            raise ValueError("capture batch ID reused with different content")
        return False
    if state["pending"] or state["errors"]:
        raise ValueError("previous capture batch is unfinished")
    tasks = batch.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("empty capture task batch")
    names = [t.get("agent_id") for t in tasks]
    if len(set(names)) != len(names) or set(names) != set(receivers):
        raise ValueError("capture batch recipients must exactly match unique tasks")
    phase = batch.get("phase")
    tool = {"observe": "sim_observe", "waypoint": "sim_move_to", "tag": "sim_try_tag"}.get(phase)
    for task in tasks:
        validate_contract(task, state["round_id"], task["agent_id"])
        if task["agent_id"] not in state["roles"] or task["tool"] != tool or task["id"] != key + ":" + task["agent_id"]:
            raise ValueError("mixed phase, unknown member or invalid capture task ID")
    if phase == "observe":
        if set(names) != set(missing_observers(state)):
            raise ValueError("observation batch must cover all required observers")
    else:
        if missing_observers(state):
            raise ValueError("all observation receipts must arrive before waypoints or tags")
        if phase == "waypoint":
            if state["tagged"] or state["waypoints_decided"]:
                raise ValueError("waypoints cannot be reassigned after that phase")
            if state["time"] == 0 and set(names) != set(state["roles"]):
                raise ValueError("initial waypoint batch must include every member, including observers")
            validate_waypoint_plan(state, {"waypoints": [
                {"agent_id": t["agent_id"], **t["arguments"], "observed_at": t["observed_at"]} for t in tasks
            ]})
        elif phase == "tag":
            if state["time"] == 0 and set(state["moved"]) != set(state["roles"]):
                raise ValueError("initial waypoint receipts missing; cannot tag")
            if set(names) != set(role_members(state, "marker")) or state["tagged"]:
                raise ValueError("tag batch must cover all markers exactly once")
        else:
            raise ValueError("unknown capture phase")
    state["batches"][key] = deepcopy(batch)
    state["pending"] = {task["agent_id"]: deepcopy(task) for task in tasks}
    if phase in {"waypoint", "tag"}:
        state["waypoints_decided"] = True
    return True


def record_result(state, sender, report):
    # A delayed duplicate may arrive while this agent has a NEW pending task.
    # Validate it against completed contracts before looking at the new one.
    for done in state["completed"].values():
        if done["agent_id"] == sender:
            try:
                decode_receipt_report(report, done)
                return False
            except (ValueError, KeyError, TypeError):
                pass
    contract = state["pending"].get(sender)
    if contract is None:
        raise ValueError("unexpected capture task result")
    result = decode_receipt_report(report, contract)
    tool = contract["tool"]
    if tool == "sim_observe":
        state["observed"].append(sender)
        if result["target_position"] is not None:
            state["observations"][sender] = deepcopy(result)
    elif tool == "sim_move_to":
        state["moved"].append(sender)
        state["waypoints"][sender] = list(result["waypoint"])
    else:
        state["tagged"].append(sender)
        state["captured"] |= result["accepted"] or result.get("reason") == "already_captured"
    state["completed"][contract["id"]] = contract
    del state["pending"][sender]
    return True


def validate_waypoint_plan(state, response):
    if not isinstance(response, dict) or not isinstance(response.get("waypoints"), list):
        raise ValueError("return a JSON object with a waypoints list")
    plans = response["waypoints"]
    names = [row.get("agent_id") for row in plans if isinstance(row, dict)]
    if len(names) != len(plans) or len(set(names)) != len(names) or any(n not in state["roles"] for n in names):
        raise ValueError("waypoint plan has duplicate or unknown members")
    if state["time"] == 0 and set(names) != set(state["roles"]):
        raise ValueError("t=0 waypoint plan must include ALL members, including both observers")
    samples = list(state["observations"].values())
    if not samples and plans:
        raise ValueError("cannot invent waypoints before a delivered visible observation")
    actions = []
    for row in plans:
        if (not all(finite_coordinate(row.get(k)) for k in ("x", "y"))
                or type(row.get("observed_at")) is not int
                or row["observed_at"] not in {s["observed_at"] for s in samples}):
            raise ValueError("waypoint needs finite x/y and an observed_at from delivered observations")
        # No route prediction: this capture pilot follows observed locations.
        if not any(row["observed_at"] == s["observed_at"] and [row["x"], row["y"]] == s["target_position"] for s in samples):
            raise ValueError("waypoint must use a delivered observed position; placeholder/predicted coordinates are invalid")
        if state["time"] and state["waypoints"].get(row["agent_id"]) == [row["x"], row["y"]]:
            continue  # Continue the accepted destination; do not invent a receipt.
        actions.append({"agent_id": row["agent_id"], "tool": "sim_move_to",
                        "arguments": {"x": row["x"], "y": row["y"]}, "observed_at": row["observed_at"]})
    return actions


async def plan_waypoints(llm, state, attempts=3):
    prompt = (
        "你是捕获队长，只规划本阶段航点，程序负责观测/航点/标记顺序。"
        "只输出 JSON：{\"waypoints\":[{\"agent_id\":\"成员ID\",\"x\":0,\"y\":40,\"observed_at\":0}]}。"
        "例子不是目标坐标，必须使用下面真实收到的观测位置及原 observed_at，不预测路线。"
        "t=0 必须为全部成员（包括所有观测者）规划；后续仅列需要改变目的地的成员，无变化填空列表。"
    )
    context = {key: state[key] for key in ("time", "roles", "observations", "waypoints")}
    feedback = ""
    for _ in range(attempts):
        reply = await llm.agenerate_response(prepend_prompt=prompt,
            append_prompt=json.dumps(context, ensure_ascii=False) + feedback,
            response_format={"type": "json_object"})
        try:
            return validate_waypoint_plan(state, reply.content)
        except (ValueError, TypeError, KeyError) as exc:
            feedback = "\n上次计划未执行。修正错误：" + str(exc)
    raise ValueError("capture waypoint plan invalid after bounded corrections: " + feedback)
