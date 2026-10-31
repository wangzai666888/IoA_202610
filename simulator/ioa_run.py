"""Run a fixed capture team through IoA one simulation second at a time.

The controller reads only role/task status and its own audit before stepping.
It never supplies target coordinates or sets agent waypoints. This is a
continuous-communication pilot, not the later communication-window experiment.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from simulator.http_client import ControllerClient
from simulator.ioa_chat import attach_chat_artifacts
from simulator.ioa_check import launch_ioa
from simulator.prepare_ioa import ROLE_IDS
from common.capture_protocol import encode_round_goal


DEFAULT_CLIENTS = {
    "UAV_observer": "http://127.0.0.1:5060",
    "UUV_leader": "http://127.0.0.1:5061",
    "USV_marker": "http://127.0.0.1:5062",
}


DEFAULT_MEMBERS = [{"id": agent_id, "role": role} for role, agent_id in ROLE_IDS.items()]


def team_roles(members: list[dict]) -> dict[str, list[str]]:
    roles = {role: [] for role in ("observer", "leader", "marker")}
    seen = set()
    for member in members:
        agent_id, role = member["id"], member["role"]
        if not isinstance(agent_id, str) or not agent_id or agent_id in seen or role not in roles:
            raise ValueError("team members need unique nonempty IDs and valid roles")
        seen.add(agent_id)
        roles[role].append(agent_id)
    if not roles["observer"] or len(roles["leader"]) != 1 or not roles["marker"]:
        raise ValueError("team needs observers, exactly one leader, and markers")
    return roles


def round_goal(episode: str, sim_time: int, observation_period: int, members=None) -> str:
    members = DEFAULT_MEMBERS if members is None else members
    roles = team_roles(members)
    observers = "、".join(roles["observer"])
    markers = "、".join(roles["marker"])
    leader = roles["leader"][0]
    if sim_time % observation_period == 0:
        observation = (
            f"先给观测者 {observers} 分配同步任务，每人实际调用 sim_observe，"
            "将坐标或未发现结果及 observed_at 经 IoA 报告给队长；等所有观测任务返回。"
        )
    else:
        observation = (
            "本秒没有新的目标采样；只可依据已通过 IoA 报告的最后一次有效观测，"
            "保留原 observed_at，不得把旧坐标说成当前精确位置，也不预测轨迹。"
        )
    first_waypoints = (
        f"本轮全部 {len(members)} 名成员必须各自实际调用一次 sim_move_to 设置初始航点。"
        if sim_time == 0 else
        "所有成员（包括队长）继续执行原航点，到达后停留；目的地不变时不要分配 sim_move_to 任务。"
        "角色描述中的自身航点义务不代表每秒重设；只在需要改变目的地时分配航点任务。"
    )
    instructions = (
        f"协同捕获仿真本轮：episode_id={episode}，expected_time={sim_time}。"
        "工具的 episode_id 和 expected_time 由本轮 IoA 执行上下文绑定，"
        "调用 sim_move_to 只填 x、y，调用 sim_try_tag 不填参数；报告中仍要如实记录工具回执。"
        f"{observation}"
        f"{first_waypoints}"
        f"队长 {leader} 根据已有报告协调航点；成员只能给自己调用 sim_move_to，"
        "已有效的上一航点会继续执行，到终点后停留。"
        f"等全部航点任务返回后，在本轮最后给标记者 {markers} 分配同步任务，"
        "每人实际调用一次 sim_try_tag，报告真实 accepted 结果；false 是正常结果。"
        "若其他标记者已经成功，后续请求返回 already_captured 是正常的终局确认，不再移动或重试。"
        "请用同步任务分配，等工具结果返回后再结束讨论。"
        "本轮不等待位置移动，不推进时钟，不替其他成员调用工具，"
        "不用 subtask_solver 代替实际操作，不请求其他团队。"
        "任务分配写明每名接收者本次动作及完整参数，航点包含 x、y 和观测的 observed_at；"
        "任务直接交给工具后端，不再转述。报告保留真实回执，说明用一句话，不重复历史和规则。"
    )
    return encode_round_goal(instructions, episode, sim_time, observation_period, members)


def round_payload(state: dict, comm_id: str | None, max_turns: int) -> dict:
    t = state["time"]
    episode = state["episode_id"]
    members = state.get("members", DEFAULT_MEMBERS)
    goal = round_goal(episode, t, state["observation_period"], members)
    payload = {
        "goal": goal,
        "max_turns": max_turns,
        "sim_round_id": f"{episode}:{t}",
        "skip_naming": True,
    }
    if comm_id is None:
        payload.update({
            "team_member_names": [member["id"] for member in members],
            "team_up_depth": 0,
            "is_collaborative_planning_enabled": False,
        })
    else:
        payload.update({
            "comm_id": comm_id,
            "cont_input": {"sender": "user", "content": goal},
        })
    return payload


def read_round_status(url: str, comm_id: str, timeout: float) -> dict | None:
    path = "/simulation/round_status?comm_id=" + quote(comm_id, safe="")
    try:
        with urlopen(url.rstrip("/") + path, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def wait_for_barrier(
    clients: dict[str, str], comm_id: str, round_id: str, deadline: float,
    poll_interval: float = 0.5, fetch=read_round_status,
) -> dict[str, dict]:
    """Return only after every participant concluded and has no unfinished task."""
    last = {}
    last_network_errors = {}
    while True:
        for agent_id, url in clients.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"round barrier timed out; last status={last}; "
                    f"last network errors={last_network_errors}"
                )
            try:
                status = fetch(url, comm_id, min(10, remaining))
            except HTTPError:
                raise
            except (TimeoutError, URLError) as exc:
                # A single status GET timeout is not evidence that the IoA round
                # failed. Keep polling within the round's wall-clock deadline.
                last[agent_id] = None
                last_network_errors[agent_id] = f"{type(exc).__name__}: {exc}"
                continue
            last_network_errors.pop(agent_id, None)
            if status is None or status.get("round_id") != round_id:
                last[agent_id] = None
                continue
            if status.get("agent_name") != agent_id or status.get("comm_id") != comm_id:
                raise RuntimeError(f"wrong IoA client or session for {agent_id}")
            if status.get("round_error"):
                raise RuntimeError(f"{agent_id}: {status['round_error']}")
            if status.get("failed_tasks"):
                raise RuntimeError(f"{agent_id} has failed tool tasks: {status['failed_tasks']}")
            last[agent_id] = status
        if len(last) == len(clients) and all(
            status is not None and status["concluded"]
            and not status["pending_tasks"] and not status["await_agents"]
            for status in last.values()
        ):
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"round barrier timed out; last status={last}; "
                f"last network errors={last_network_errors}"
            )
        time.sleep(min(poll_interval, remaining))


def verify_round_audit(audit: dict, episode: str, sim_time: int, period: int, members=None) -> list[str]:
    members = DEFAULT_MEMBERS if members is None else members
    roles = team_roles(members)
    if audit["episode_id"] != episode or audit["time"] != sim_time:
        raise RuntimeError("simulator audit is from another episode or simulation time")
    calls = {
        (call["agent_id"], call["operation"])
        for call in audit["calls"] if call["time"] == sim_time
    }
    accepted = {
        call["agent_id"] for call in audit["calls"]
        if call["time"] == sim_time and call["operation"] == "tag" and call.get("accepted")
    }
    captured = audit.get("status") == "captured"
    if captured and not accepted.intersection(roles["marker"]):
        return ["captured status has no accepted marker request in this round"]
    required = set() if captured else {(agent, "tag") for agent in roles["marker"]}
    if sim_time % period == 0:
        required.update((agent, "observe") for agent in roles["observer"])
    if sim_time == 0:
        required.update((member["id"], "waypoint") for member in members)
    missing = [f"{agent}:{operation}" for agent, operation in sorted(required - calls)]
    if missing:
        return missing
    ordered = [
        (call["agent_id"], call["operation"])
        for call in audit["calls"] if call["time"] == sim_time
    ]
    tag_index = min(i for i, (agent, op) in enumerate(ordered) if agent in roles["marker"] and op == "tag")
    if sim_time % period == 0:
        if any(ordered.index((agent, "observe")) > tag_index for agent in roles["observer"]):
            return ["observer observation arrived after marker tag"]
    if sim_time == 0:
        # Parallel IoA tasks can produce an early unsuccessful tag while other
        # initial waypoints are still being submitted. Movement is frozen until
        # the controller steps. Require a later real tag from each marker after
        # every initial waypoint, rather than rejecting on the first tag alone.
        last_waypoint_index = max(
            ordered.index((member["id"], "waypoint")) for member in members
        )
        markers_to_check = accepted if captured else roles["marker"]
        for marker_id in markers_to_check:
            if not any(
                index > last_waypoint_index and agent == marker_id and operation == "tag"
                for index, (agent, operation) in enumerate(ordered)
            ):
                return [f"{marker_id} has no tag after all initial waypoints"]
    return []


def run_episode(
    controller: ControllerClient,
    clients: dict[str, str],
    *,
    max_rounds: int = 3,
    max_turns: int | None = None,
    round_timeout: float = 900,
    expected_members: list[dict] | None = None,
    launch=launch_ioa,
    fetch=read_round_status,
    print_progress=print,
) -> dict:
    if max_rounds < 0 or (max_turns is not None and max_turns < 1) or round_timeout <= 0:
        raise ValueError("max_rounds >= 0, max_turns >= 1 and round_timeout > 0 are required")
    state = controller.reset()
    episode = state["episode_id"]
    actual_members = {(m["id"], m["role"]) for m in state["members"]}
    comm_id = None
    rounds = []
    error = None
    failure_context = None
    phase = "initialization"
    sim_time = state["time"]
    round_id = None
    round_started_at = None
    try:
        roles = team_roles(state["members"])
        if set(clients) != {m["id"] for m in state["members"]}:
            raise ValueError("client map must exactly match the simulator's selected team")
        if expected_members is not None and actual_members != {(m["id"], m["role"]) for m in expected_members}:
            raise ValueError("client configuration roles differ from the simulator's selected team")
        if max_turns is None:
            max_turns = 12 if len(state["members"]) == 3 else 24
        while state["status"] == "running":
            if max_rounds and len(rounds) >= max_rounds:
                controller.abort(episode, state["time"], f"planned stop after {max_rounds} rounds")
                break
            sim_time = state["time"]
            round_started_at = time.monotonic()
            deadline = time.monotonic() + round_timeout
            payload = round_payload(state, comm_id, max_turns)
            round_id = payload["sim_round_id"]
            print_progress(f"simulation second {sim_time}: starting IoA round {round_id}")
            phase = "launch_goal"
            reply = launch(clients[roles["leader"][0]], payload, max(0.1, deadline - time.monotonic()))
            if (not isinstance(reply, list) or len(reply) != 2
                    or not isinstance(reply[0], str) or not reply[0]
                    or not isinstance(reply[1], str) or not reply[1]):
                raise RuntimeError(f"invalid IoA launch_goal reply: {reply!r}")
            if comm_id is not None and reply[0] != comm_id:
                raise RuntimeError("IoA changed the communication session during continuation")
            comm_id = reply[0]
            phase = "round_barrier"
            statuses = wait_for_barrier(
                clients, comm_id, round_id, deadline, fetch=fetch,
            )
            phase = "tool_audit"
            audit = controller.audit()
            missing = verify_round_audit(audit, episode, sim_time, state["observation_period"], state["members"])
            if missing:
                raise RuntimeError(f"required tool calls missing at t={sim_time}: {missing}")
            phase = "controller_status"
            current = controller.status()
            if current["episode_id"] != episode or current["time"] != sim_time:
                raise RuntimeError("another controller advanced or replaced the episode")
            round_record = {
                "time": sim_time, "round_id": round_id, "comm_id": comm_id,
                "conclusion": reply[1], "participants": statuses,
                "elapsed_wall_seconds": round(time.monotonic() - round_started_at, 1),
                "tool_calls": [
                    call for call in audit["calls"] if call["time"] == sim_time
                ],
            }
            rounds.append(round_record)
            print_progress(
                f"simulation second {sim_time}: tasks and tool calls verified "
                f"({round_record['elapsed_wall_seconds']} wall-clock seconds)"
            )
            if current["status"] != "running":
                state = current
                break
            if sim_time >= current["max_time"]:
                phase = "finish"
                state = controller.finish(episode, sim_time)
            else:
                phase = "step"
                state = controller.step(episode, sim_time)
    except (Exception, KeyboardInterrupt) as exc:
        elapsed = None if round_started_at is None else round(time.monotonic() - round_started_at, 1)
        failure_context = {
            "phase": phase, "round_id": round_id, "sim_time": sim_time,
            "elapsed_wall_seconds": elapsed,
        }
        if comm_id is not None:
            client_status = {}
            for agent_id, url in clients.items():
                try:
                    client_status[agent_id] = fetch(url, comm_id, 2)
                except Exception as status_exc:
                    client_status[agent_id] = {
                        "status_error": f"{type(status_exc).__name__}: {status_exc}"
                    }
            failure_context["client_status"] = client_status
        try:
            audit = controller.audit()
            failure_context["tool_calls_this_second"] = [
                {"agent_id": call["agent_id"], "operation": call["operation"]}
                for call in audit["calls"] if call["time"] == sim_time
            ]
        except Exception as audit_exc:
            failure_context["audit_error"] = f"{type(audit_exc).__name__}: {audit_exc}"
        error = f"{type(exc).__name__} during {phase} at t={sim_time} after {elapsed}s: {exc}"
        print_progress(f"simulation second {sim_time}: stopped during {phase}; {type(exc).__name__}: {exc}")
    finally:
        current = controller.status()
        if current["episode_id"] != episode:
            raise RuntimeError("another controller replaced the active episode")
        if current["status"] == "running":
            controller.abort(episode, current["time"], error or "incomplete IoA round")
    result = controller.result()
    return {
        "kind": "fixed_team_continuous_ioa_pilot",
        "episode_id": episode,
        "scenario_id": result["scenario_id"],
        "members": state["members"],
        "team_selection": result.get("team_selection"),
        "max_turns": max_turns,
        "comm_id": comm_id,
        "status": result["status"],
        "capture_time": result["capture_time"],
        "time": result["time"],
        "completed_rounds": len(rounds),
        "rounds": rounds,
        "error": error,
        "failure_context": failure_context,
        "artifacts": result["artifacts"],
    }


def main() -> None:
    root = Path(__file__).parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim-url", default="http://127.0.0.1:8090")
    parser.add_argument("--ioa-server-url", default="http://127.0.0.1:7788",
                        help="IoA Server address for exporting this session's group messages")
    parser.add_argument("--observer-url", default=DEFAULT_CLIENTS["UAV_observer"])
    parser.add_argument("--leader-url", default=DEFAULT_CLIENTS["UUV_leader"])
    parser.add_argument("--marker-url", default=DEFAULT_CLIENTS["USV_marker"])
    parser.add_argument("--clients-file", type=Path,
                        help="generated five_clients.local.json; overrides the three role URLs")
    parser.add_argument("--auth-file", type=Path, default=root / "service_auth.local.json")
    parser.add_argument("--output-dir", type=Path, default=root / "outputs")
    parser.add_argument("--max-rounds", type=int, default=3,
                        help="3 by default for a small model smoke run; 0 runs until a terminal outcome")
    parser.add_argument("--max-turns", type=int,
                        help="defaults to 12 for three members, 24 for five members")
    parser.add_argument("--round-timeout", type=float, default=900,
                        help="wall-clock seconds per IoA round; never simulation time")
    parser.add_argument("--abort-active", action="store_true")
    args = parser.parse_args()
    auth = json.loads(args.auth_file.read_text(encoding="utf-8"))
    controller = ControllerClient(args.sim_url, auth["controller_token"])
    if args.abort_active:
        state = controller.status()
        if state["status"] == "running":
            controller.abort(state["episode_id"], state["time"], "operator cleanup after interrupted IoA run")
            print("Aborted the active episode. Restart capture clients before a new run.")
        else:
            print("No running episode to abort.")
        return
    clients = {
        "UAV_observer": args.observer_url,
        "UUV_leader": args.leader_url,
        "USV_marker": args.marker_url,
    }
    expected_members = None
    if args.clients_file:
        manifest = json.loads(args.clients_file.read_text(encoding="utf-8"))
        clients = manifest["clients"]
        expected_members = manifest["members"]
        team_roles(expected_members)
        if (set(clients) != {m["id"] for m in expected_members}
                or len(set(clients.values())) != len(clients)):
            raise ValueError("clients file needs one distinct endpoint per selected member")
    for url in clients.values():
        with urlopen(url.rstrip("/") + "/openapi.json", timeout=10) as response:
            paths = json.load(response).get("paths", {})
            if "/simulation/round_status" not in paths or "/launch_goal" not in paths:
                raise RuntimeError(f"{url} lacks the required IoA round endpoints; rebuild the client image")
    report = run_episode(
        controller, clients, max_rounds=args.max_rounds,
        max_turns=args.max_turns, round_timeout=args.round_timeout,
        expected_members=expected_members,
    )
    attach_chat_artifacts(report, args.ioa_server_url, args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f'{report["episode_id"]}.ioa-run.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "episode_id", "comm_id", "status", "capture_time",
        "time", "completed_rounds", "error", "failure_context",
        "chat_message_count", "chat_export_error", "artifacts",
    )}, ensure_ascii=False, indent=2))
    print(f"Report: {path}")
    if report["error"] or report["chat_export_error"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
