"""Check actual IoA tool wiring at frozen t=0, then abort and export evidence.

This calls the configured models through IoA. It is not a full-episode scheduler.
It never reads an observer's data or chooses waypoints on the controller's behalf.
"""

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

from simulator.http_client import ControllerClient
from simulator.prepare_ioa import ROLE_IDS
from common.capture_protocol import encode_round_goal


def launch_ioa(url: str, payload: dict, timeout: float):
    request = Request(
        url.rstrip("/") + "/launch_goal",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    # A timeout does NOT cancel the work in the remote IoA processes.
    # Do not retry this request automatically.
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def goal_payload(episode: str, max_turns: int) -> dict:
    payload = {
        "goal": (
            f"三角色工具接线检查：本轮 episode_id={episode}，expected_time=0。"
            "仿真时钟固定在0秒，本轮只处理一次工具链，不等待位置移动。"
            "先给 UAV_observer 分配同步工具任务：实际调用 sim_self_state 和 sim_observe，"
            "将目标坐标、observed_at 和工具返回值通过本组 IoA 消息报告给 UUV_leader。"
            "然后 UUV_leader 根据该报告安排三人的航点，不预测轨迹；若看不到目标则报告失败。"
            "分别给 UAV_observer、UUV_leader、USV_marker 分配同步工具任务，"
            "让每人实际调用 sim_move_to 设置自身航点；工具的 episode_id 和 expected_time=0 "
            "由 IoA 执行上下文绑定，模型调用时只填 x、y。"
            "最后让 USV_marker 实际调用 sim_try_tag 并报告 accepted，false 是此时的正常结果。"
            "所有任务返回后结束讨论；不能用自然语言陈述或 subtask_solver 代替实际工具调用。"
            "不得组建其他团队，不得请求其他成员，不得重复等待到达或重复尝试标记。"
        ),
        "team_member_names": list(ROLE_IDS.values()),
        "sim_round_id": f"{episode}:0",
        "team_up_depth": 0,
        "max_turns": max_turns,
        "skip_naming": True,
        "is_collaborative_planning_enabled": False,
    }
    payload["goal"] = encode_round_goal(payload["goal"], episode, 0, 5,
        [{"id": agent, "role": role} for role, agent in ROLE_IDS.items()])
    return payload


def assess(result: dict, reply, error: str | None) -> dict:
    calls = result.get("tool_audit", [])
    required = {
        ("UAV_observer", "observe"),
        ("UAV_observer", "waypoint"),
        ("UUV_leader", "waypoint"),
        ("USV_marker", "waypoint"),
        ("USV_marker", "tag"),
    }
    actual = {(call["agent_id"], call["operation"]) for call in calls if call["time"] == 0}
    missing = [f"{agent}:{operation}" for agent, operation in sorted(required - actual)]
    reply_ok = (
        isinstance(reply, list) and len(reply) == 2
        and isinstance(reply[0], str) and bool(reply[0])
        and isinstance(reply[1], str) and bool(reply[1])
    )
    passed = (
        error is None and reply_ok and not missing
        and result["time"] == 0 and result["status"] == "aborted"
    )
    return {
        "kind": "single_tick_tool_wiring_check",
        "episode_id": result["episode_id"],
        "tool_check_passed": passed,
        "time": result["time"],
        "status": result["status"],
        "missing_tool_calls": missing,
        "error": error,
        "ioa_reply": reply,
        "tool_audit": calls,
        "artifacts": result.get("artifacts"),
        "manual_review_required": (
            "Inspect the IoA conversation: observer coordinates and observed_at must "
            "reach the leader, then the leader's commands must reach the members. "
            "API call presence alone does not prove correct collaboration."
        ),
    }


def run_check(controller: ControllerClient, leader_url: str, timeout: float = 900,
              max_turns: int = 12, launch=launch_ioa) -> dict:
    state = controller.reset()
    episode = state["episode_id"]
    reply, error = None, None
    try:
        expected = {(agent_id, role) for role, agent_id in ROLE_IDS.items()}
        actual = {(member["id"], member["role"]) for member in state["members"]}
        if actual != expected:
            raise ValueError("ioa_check requires the three-member pilot scenario")
        print(f"episode_id={episode}; simulation time is frozen at 0.", flush=True)
        reply = launch(leader_url, goal_payload(episode, max_turns), timeout)
    except (Exception, KeyboardInterrupt) as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        current = controller.status()
        if current["episode_id"] != episode:
            raise RuntimeError("another controller changed the episode; do not run controllers concurrently")
        if current["status"] == "running":
            controller.abort(episode, current["time"], "single-tick integration check" if error is None else
                             "single-tick integration check interrupted")
    return assess(controller.result(), reply, error)


def main():
    root = Path(__file__).parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim-url", default="http://127.0.0.1:8090")
    parser.add_argument("--leader-url", default="http://127.0.0.1:5061")
    parser.add_argument("--observer-url", default="http://127.0.0.1:5060")
    parser.add_argument("--marker-url", default="http://127.0.0.1:5062")
    parser.add_argument("--auth-file", type=Path, default=root / "service_auth.local.json")
    parser.add_argument("--output-dir", type=Path, default=root / "outputs")
    parser.add_argument("--timeout", type=float, default=900,
                        help="wall-clock seconds; this does not advance simulation time")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--abort-active", action="store_true",
                        help="only abort a previously interrupted episode; do not launch IoA")
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_turns <= 0:
        parser.error("--timeout and --max-turns must be positive")
    auth = json.loads(args.auth_file.read_text(encoding="utf-8"))
    controller = ControllerClient(args.sim_url, auth["controller_token"])
    if args.abort_active:
        state = controller.status()
        if state["status"] == "running":
            controller.abort(state["episode_id"], state["time"], "operator cleanup after interrupted check")
            print("Aborted the active simulation. Restart capture clients before launching another check.")
        else:
            print("No running simulation to abort.")
        return
    # Readiness checks do not invoke a model or reset the simulator.
    for url in (args.observer_url, args.leader_url, args.marker_url):
        with urlopen(url.rstrip("/") + "/openapi.json", timeout=10) as response:
            if "/launch_goal" not in json.load(response).get("paths", {}):
                raise RuntimeError(f"{url} is not the expected IoA client")
    report = run_check(controller, args.leader_url, args.timeout, args.max_turns)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f'{report["episode_id"]}.ioa-check.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "episode_id", "tool_check_passed", "time", "status", "missing_tool_calls", "error",
    )}, ensure_ascii=False, indent=2))
    print(f"Report: {path}")
    print("Review the IoA conversation as well. This check does not establish a full capture loop.")
    print("Restart the three capture clients before another check, especially after a timeout.")
    raise SystemExit(0 if report["tool_check_passed"] else 1)


if __name__ == "__main__":
    main()
