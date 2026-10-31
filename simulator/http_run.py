"""Run the same deterministic pilot through real HTTP, without an LLM.

This trusted fixture holds all test tokens to exercise each local tool.
It is not the IoA experiment controller or evidence of IoA collaboration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from simulator.http_client import AgentClient, ControllerClient


def run_http_episode(base_url: str, auth: dict) -> dict:
    controller = ControllerClient(base_url, auth["controller_token"])
    state = controller.reset()
    episode = state["episode_id"]
    clients = {
        member["id"]: AgentClient(base_url, auth["agent_tokens"][member["id"]])
        for member in state["members"]
    }
    observers = [m["id"] for m in state["members"] if m["role"] == "observer"]
    markers = [m["id"] for m in state["members"] if m["role"] == "marker"]
    while state["status"] == "running":
        current_time = state["time"]
        if current_time % state["observation_period"] == 0:
            samples = [clients[member].observe() for member in observers]
            visible = [s for s in samples if s["target_position"] is not None]
            if visible:
                newest = max(visible, key=lambda sample: sample["observed_at"])
                for client in clients.values():
                    client.set_waypoint(episode, current_time, newest["target_position"])
        for marker in markers:
            response = clients[marker].try_tag(episode, current_time)
            if response["accepted"]:
                return controller.result()
        if current_time == state["max_time"]:
            state = controller.finish(episode, current_time)
        else:
            state = controller.step(episode, current_time)
    return controller.result()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8090")
    parser.add_argument(
        "--auth-file", type=Path,
        default=Path(__file__).with_name("service_auth.local.json"),
    )
    args = parser.parse_args()
    auth = json.loads(args.auth_file.read_text(encoding="utf-8"))
    result = run_http_episode(args.url, auth)
    print(json.dumps({
        "episode_id": result["episode_id"],
        "status": result["status"],
        "capture_time": result["capture_time"],
        "trace_frames": len(result["trace"]),
        "artifacts": result["artifacts"],
    }, indent=2))


if __name__ == "__main__":
    main()
