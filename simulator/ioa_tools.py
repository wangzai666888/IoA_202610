"""Agent-only tool adapters, ready for registration in the IoA ReAct backend.

No controller token, simulator state object, or scenario file is needed.
The communication/task layer supplies the current episode_id and time.
"""

import argparse
import json
import os
from pathlib import Path
import re

from simulator.http_client import AgentClient


ROUND_ID = re.compile(r"([0-9a-f]{32}):([0-9]{1,3})\Z")


def bind_simulation_tool_args(name: str, arguments: dict, sim_round_id: str | None) -> dict:
    """Bind mutation identity to the IoA round, never to model-written text."""
    if not isinstance(arguments, dict):
        raise ValueError("simulation tool arguments must be an object")
    if name not in {"sim_move_to", "sim_try_tag"}:
        return arguments
    match = ROUND_ID.fullmatch(sim_round_id or "")
    if match is None:
        raise ValueError("a valid simulation round is required for mutation tools")
    # Older prompts may still make the model supply these fields. Discard them:
    # the immutable task round is the authority, including for delayed tasks.
    supplied = set(arguments) - {"episode_id", "expected_time"}
    allowed = {"x", "y"} if name == "sim_move_to" else set()
    if supplied != allowed:
        raise ValueError(f"{name} requires exactly {sorted(allowed)} model arguments")
    return {
        "episode_id": match.group(1),
        "expected_time": int(match.group(2)),
        **{key: arguments[key] for key in allowed},
    }


def _client() -> AgentClient:
    return AgentClient(
        os.environ.get("SIM_URL", "http://ioa-simulator:8090"),
        os.environ["SIM_AGENT_TOKEN"],
    )


def sim_self_state() -> str:
    return json.dumps(_client().state(), ensure_ascii=False)


def sim_observe() -> str:
    return json.dumps(_client().observe(), ensure_ascii=False)


def sim_move_to(episode_id: str, expected_time: int, x: float, y: float) -> str:
    return json.dumps(
        _client().set_waypoint(episode_id, expected_time, [x, y]),
        ensure_ascii=False,
    )


def sim_try_tag(episode_id: str, expected_time: int) -> str:
    return json.dumps(
        _client().try_tag(episode_id, expected_time), ensure_ascii=False
    )


TOOL_FUNCTIONS = {
    "sim_self_state": sim_self_state,
    "sim_observe": sim_observe,
    "sim_move_to": sim_move_to,
    "sim_try_tag": sim_try_tag,
}


def tool_schemas(role: str) -> list[dict]:
    definitions = {
        "sim_self_state": ("读取自身状态及本局时刻，不包含其他成员或目标真值。", {}),
        "sim_observe": (
            "仅观测者可调用。返回最近一次本地采样，保留 observed_at；目标不在观测范围时坐标为 null。",
            {},
        ),
        "sim_move_to": (
            "设置自身持续航点。episode_id 和仿真秒由 IoA 任务上下文绑定，无需填写；不能替其他成员设置航点。",
            {
                "x": {"type": "number", "description": "航点 x 坐标，米。"},
                "y": {"type": "number", "description": "航点 y 坐标，米。"},
            },
        ),
        "sim_try_tag": (
            "仅标记者可调用。episode_id 和仿真秒由 IoA 任务上下文绑定，无需填写；以 accepted 判断成功。",
            {},
        ),
    }
    by_role = {
        "observer": ["sim_self_state", "sim_observe", "sim_move_to"],
        "leader": ["sim_self_state", "sim_move_to"],
        "marker": ["sim_self_state", "sim_move_to", "sim_try_tag"],
    }
    if role not in by_role:
        raise ValueError("role must be observer, leader or marker")
    schemas = []
    for name in by_role[role]:
        description, properties = definitions[name]
        schemas.append({
            "type": "function",
            "function": {
                "name": name, "description": description,
                "parameters": {
                    "type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False,
                },
            },
        })
    return schemas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for role in ("observer", "leader", "marker"):
        (args.output_dir / f"{role}.json").write_text(
            json.dumps(tool_schemas(role), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
