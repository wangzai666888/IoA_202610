"""Five-agent window experiment with identical local scheduling in all conditions.

The controller schedules tasks and attests acknowledged authorization delivery.
Agent reports and commands travel through the IoA Server's per-recipient queues;
the controller never supplies agents with observations or physical truth.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from common.communication_windows import PROTOCOL_VERSION, pair_catalog, profile_from_config
from simulator.http_client import ControllerClient
from simulator.ioa_chat import attach_chat_artifacts
from simulator.ioa_run import team_roles, verify_round_audit
from simulator.scenario import load_scenario
from simulator.fixed_scenarios import fixed_scene, scene_members
from simulator.experiment_rules import RULES, frozen_rules
from simulator.authorization_metrics import authorization_timeline


def post_json(url, body, timeout=30, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    request = Request(url, data=json.dumps(body, allow_nan=False).encode(), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc


def experiment_snapshot(root, *, scenario_path=None, client_configs=None):
    """Fingerprint source/prompts and public model settings, never credentials."""
    paths = [*root.glob("common/*.py"), *root.glob("common/types/*.py"),
             *root.glob("im_client/communication/*.py"), *root.glob("im_client/prompts/*.py"),
             *root.glob("im_client/llms/*.py"), *root.glob("im_client/agents/react/*.py"),
             *root.glob("simulator/*.py"), root / "im_server/app.py", root / "im_client/main.py",
             Path(scenario_path) if scenario_path is not None else root / "simulator/scenarios/five_team.json"]
    models = {}
    config_paths = (list(client_configs) if client_configs is not None else
                    root.glob("configs/client_configs/cases/capture_five.local/*.yaml"))
    for path in config_paths:
        path = Path(path)
        config = json.loads(path.read_text(encoding="utf-8"))
        models[path.stem] = {"comm_model": config["comm"]["llm"],
                            "tool_model": config["tool_agent"]["model"],
                            "max_num_steps": config["tool_agent"]["max_num_steps"]}
        paths.append(path)
    return {"source_sha256": {(p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p)):
                              hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(set(paths)) if p.is_file()}, "models": models}


def verify_scenario_identity(state, expected, members):
    if (state.get("scenario_id") != expected["scenario_id"]
            or state.get("scenario_sha256") != expected["scenario_sha256"]):
        raise ValueError("simulator scenario ID/SHA-256 differs from the selected frozen scene; "
                         "rebuild the simulator image and start this scene's Compose bundle")
    if {(m["id"], m["role"], m["carrier_type"]) for m in state["members"]} != {
            (m["id"], m["role"], m["carrier_type"]) for m in members}:
        raise ValueError("simulator selected members differ from the frozen scene bundle")


def run_window_episode(controller, clients, server_url, *, expected_members, config=None,
                       max_rounds=0, round_timeout=1800, request=post_json, print_progress=print,
                       expected_scenario=None):
    config = config or {}
    profile = profile_from_config(expected_members, config)
    if max_rounds < 0 or round_timeout <= 0 or set(clients) != {m["id"] for m in expected_members}:
        raise ValueError("invalid window run options/client map")
    leader = team_roles(expected_members)["leader"][0]
    if expected_scenario is not None:
        # Detect wrong scene (even with the same five IDs) before reset or LLM work.
        verify_scenario_identity(controller.scenario(), expected_scenario, expected_members)
    key = secrets.token_urlsafe(32)
    state = controller.reset(experiment_rules=frozen_rules())
    episode, comm_id, stage = state["episode_id"], None, "initialization"
    records, error, cleanup_errors, transport, local_events = [], None, [], None, {}
    started = time.monotonic()
    statuses = {}
    pool = ThreadPoolExecutor(max_workers=len(clients))

    def server(operation, timeout=10, **kwargs):
        return request(server_url.rstrip("/") + "/simulation/window/control",
                       {"comm_id": comm_id, "operation": operation, **kwargs}, timeout, key)

    def each(operation, deadline, **kwargs):
        futures = {name: pool.submit(request, url.rstrip("/") + "/simulation/window/control",
            {"comm_id": comm_id, "operation": operation, **kwargs},
            max(0.1, deadline - time.monotonic()), key) for name, url in clients.items()}
        result = {name: future.result() for name, future in futures.items()}
        for name, row in result.items():
            if row.get("agent_name") != name or row.get("comm_id") != comm_id or row.get("episode_id") != episode:
                raise RuntimeError("window client identity mismatch")
            if operation != "close" and row.get("error"):
                raise RuntimeError(f"{name}: {row['error']}")
        return result

    def drain(deadline):
        expected = {name: row["sent_count"] for name, row in statuses.items()}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("window transport did not ingest/acknowledge eligible messages before deadline")
            row = server("sync", min(10, remaining), expected_sends=expected)
            if row.get("error"):
                raise RuntimeError(row["error"])
            if row["settled"]:
                return row
            each("status", deadline)  # Surface receiver validation errors promptly.
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    try:
        frozen_rules(state.get("experiment_rules"))
        if state.get("experiment_rules") != RULES or state["observation_period"] != RULES["observation_period"]:
            raise ValueError("simulator has not activated the frozen authorization experiment rules")
        if expected_scenario is not None:
            verify_scenario_identity(state, expected_scenario, expected_members)
        if {(m["id"], m["role"]) for m in state["members"]} != {(m["id"], m["role"]) for m in expected_members}:
            raise ValueError("selected simulator team differs from the client manifest")
        # Resolve against the actual selected team; the earlier call only
        # validates configuration before creating a simulator episode.
        profile = profile_from_config(state["members"], config)
        print_progress(f"pair={profile['pair_id']}; restricted={profile['restricted']}; "
                       "synchronous window: 5s open / 5s closed, period=10s, phase=0; observation=3s; authorized_capture_v1")
        reply = request(server_url.rstrip("/") + "/teamup",
                        {"sender": leader, "agent_names": [n for n in clients if n != leader],
                         "team_name": "capture-window-" + episode}, 30)
        comm_id = reply["comm_id"]
        if set(reply["agent_names"]) != set(clients):
            raise ValueError("IoA Server did not form the selected team")
        request(server_url.rstrip("/") + "/simulation/window/configure",
                {"comm_id": comm_id, "episode_id": episode, "profile": profile, "key": key}, 30)
        for name, url in clients.items():
            row = request(url.rstrip("/") + "/simulation/window/init",
                {"comm_id": comm_id, "episode_id": episode, "members": state["members"],
                 "observation_period": state["observation_period"], "profile": profile, "key": key}, 30)
            if row.get("agent_name") != name:
                raise ValueError("wrong window client endpoint")
        while state["status"] == "running":
            second = state["time"]
            if max_rounds and len(records) >= max_rounds:
                controller.abort(episode, second, f"planned window stop after {max_rounds} rounds")
                break
            round_start = time.monotonic()
            deadline = round_start + round_timeout
            print_progress(f"simulation second {second}: window condition={profile['condition']}")
            for phase in ("begin", "uplink", "observe", "position", "plan", "move", "authorize", "tag", "finish"):
                stage = phase
                if phase == "tag":
                    # Register only actual ACKed marker deliveries. This control
                    # path gives agents no data and never creates permission.
                    evidence = server("authorizations", max(0.1, deadline - time.monotonic()))
                    controller.register_authorizations(episode, second, evidence["authorization_receipts"])
                statuses = each("tick", deadline, time=second, phase=phase)
                if any(row["time"] != second or row["phase"] != phase or row["busy"] for row in statuses.values()):
                    raise RuntimeError("local tick did not finish in the requested phase")
                if phase == "begin":
                    # Clients enter the new tick before any old queued data is released.
                    server("begin", max(0.1, deadline - time.monotonic()), time=second)
                queue = drain(deadline)
            stage = "tool_audit"
            audit = controller.audit()
            missing = verify_round_audit(audit, episode, second, state["observation_period"], state["members"])
            if missing:
                raise RuntimeError(f"window local tool audit failed at t={second}: {missing}")
            current = controller.status()
            if current["episode_id"] != episode or current["time"] != second:
                raise RuntimeError("another controller advanced or replaced this window episode")
            server("seal", max(0.1, deadline - time.monotonic()),
                   expected_sends={name: row["sent_count"] for name, row in statuses.items()})
            records.append({"time": second, "round_id": f"{episode}:{second}", "participants": statuses,
                "elapsed_wall_seconds": round(time.monotonic() - round_start, 1), "queue": queue,
                "uplink_queued": sum(row["uplink_queued"] for row in statuses.values()),
                "tool_calls": [c for c in audit["calls"] if c["time"] == second]})
            print_progress(f"simulation second {second}: local tasks verified; downlink_queued={queue['queued']}; "
                           f"uplink_queued={records[-1]['uplink_queued']} "
                           f"({records[-1]['elapsed_wall_seconds']} wall-clock seconds)")
            if current["status"] != "running":
                break
            stage = "step"
            state = (controller.finish(episode, second) if second >= current["max_time"]
                     else controller.step(episode, second))
    except (Exception, KeyboardInterrupt) as exc:
        error = f"{type(exc).__name__} during {stage} at t={state['time']}: {exc}"
        print_progress(error)
    finally:
        try:
            current = controller.status()
            if current["episode_id"] == episode and current["status"] == "running":
                controller.abort(episode, current["time"], error or "incomplete window run")
        except Exception as exc:
            cleanup_errors.append(f"simulator stop: {exc}")
        if comm_id is not None:
            # Stop routing before exporting: closed-window backlog stays in the
            # evidence, never released after capture just to make queues empty.
            try:
                server("close", 5)
                transport = server("export", 5)["transport"]
            except Exception as exc:
                cleanup_errors.append(f"server export: {exc}")
            for name, url in clients.items():
                try:
                    request(url.rstrip("/") + "/simulation/window/control",
                            {"comm_id": comm_id, "operation": "close"}, 5, key)
                    local_events[name] = request(url.rstrip("/") + "/simulation/window/control",
                            {"comm_id": comm_id, "operation": "export"}, 5, key)
                except Exception as exc:
                    cleanup_errors.append(f"{name} export: {exc}")
        pool.shutdown(wait=True)
    try:
        result = controller.result()
    except Exception as exc:
        cleanup_errors.append(f"simulator result: {exc}")
        result = {**state, "status": "unknown", "capture_time": None, "artifacts": {}}
    leader_known = local_events.get(leader, {}).get("capture_known_at")
    return {"kind": "five_team_communication_windows_v3", "episode_id": episode, "comm_id": comm_id,
            "experiment_rules": frozen_rules(),
            "authorizations": authorization_timeline(transport, local_events, result.get("tool_audit", []), result["time"]),
            "profile": profile, "members": profile["members"],
            "team_selection": result.get("team_selection"), "scenario_id": result.get("scenario_id"),
            "scenario_sha256": result.get("scenario_sha256"), "generation": result.get("generation"),
            "status": result["status"], "time": result["time"], "capture_time": result["capture_time"],
            "leader_capture_known_at": leader_known, "leader_informed_at_end": leader_known is not None,
            "completed_rounds": len(records), "rounds": records, "error": error,
            "elapsed_wall_seconds": round(time.monotonic() - started, 1), "cleanup_errors": cleanup_errors,
            "transport": transport, "local_events": local_events, "artifacts": result["artifacts"]}


def cli_config(config, *, condition=None, pair_id=None):
    config = dict(config)
    if pair_id is not None:
        config["pair_id"] = pair_id
        config.pop("restricted", None)
        if condition is None:
            config.pop("condition", None)  # Derive C1/C2 from the explicitly chosen pair.
    if condition is not None:
        config["condition"] = condition
        if condition == "continuous" and pair_id is None:
            config.pop("pair_id", None)
            config.pop("restricted", None)
    return config


def check_window_protocol(url):
    try:
        with urlopen(url.rstrip("/") + "/simulation/window/capabilities", timeout=10) as response:
            version = json.load(response).get("protocol_version")
    except Exception as exc:
        raise RuntimeError(f"{url} lacks window protocol v{PROTOCOL_VERSION}; rebuild/recreate server and clients") from exc
    if version != PROTOCOL_VERSION:
        raise RuntimeError(f"{url} uses window protocol {version}, expected {PROTOCOL_VERSION}; rebuild/recreate it")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=int, choices=range(1, 11), help="use one frozen scene and its prepared five-client bundle")
    parser.add_argument("--clients-file", type=Path)
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--window-config", type=Path, default=Path("simulator/scenarios/communication_windows.json"))
    parser.add_argument("--condition", choices=("continuous", "nonleader_pair", "leader_pair"))
    parser.add_argument("--pair", dest="pair_id", type=int, choices=range(1, 11),
                        help="select one of the 10 fixed member pairs; implies its C1/C2 condition")
    parser.add_argument("--list-pairs", action="store_true", help="print the 10 pairs without starting services or models")
    parser.add_argument("--max-rounds", type=int, default=25)
    parser.add_argument("--round-timeout", type=float, default=1800)
    parser.add_argument("--sim-url", default="http://127.0.0.1:8090")
    parser.add_argument("--ioa-server-url", default="http://127.0.0.1:7788")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if args.scene is not None:
        if args.clients_file is not None or args.auth_file is not None:
            parser.error("--scene supplies its own clients/auth files; do not combine these options")
        _, _, frozen = fixed_scene(args.scene, root)
        if args.list_pairs:
            print(f"scene={args.scene:02d}; scenario_id={frozen['scenario_id']}")
            for row in frozen["pairs"]:
                print(f"{row['pair_id']:2d}  {row['restricted'][0]} + {row['restricted'][1]}  ({row['condition']})")
            return
        directory = root / "simulator/fixed_runtime" / f"scene_{args.scene:02d}"
        args.clients_file, args.auth_file = directory / "clients.json", directory / "auth.local.json"
        if not args.clients_file.is_file() or not args.auth_file.is_file():
            parser.error("prepare scene bundles first: python3 -B -m simulator.prepare_fixed_scenarios")
        args.output_dir = args.output_dir or root / "simulator/outputs" / f"scene_{args.scene:02d}"
    args.clients_file = args.clients_file or Path("simulator/five_clients.local.json")
    args.auth_file = args.auth_file or Path("simulator/service_auth_five.local.json")
    args.output_dir = args.output_dir or Path("simulator/outputs")
    manifest = json.loads(args.clients_file.read_text(encoding="utf-8"))
    expected_scenario, scenario_path, config_paths = None, None, None
    if args.scene is not None or manifest.get("bundle_version") == 1:
        scenario_path = root / manifest["scenario_file"]
        env = load_scenario(scenario_path)
        expected_scenario = {"scenario_id": manifest["scenario_id"], "scenario_sha256": manifest["scenario_sha256"]}
        verify_scenario_identity({"scenario_id": env.scenario_id, "scenario_sha256": env.scenario_sha256,
                                  "members": scene_members(env)}, expected_scenario, manifest["members"])
        if args.scene is not None and expected_scenario != {k: frozen[k] for k in expected_scenario}:
            parser.error("client bundle belongs to another frozen scene")
        if set(manifest["clients"]) != {m["id"] for m in manifest["members"]}:
            parser.error("bundle client IDs differ from the selected members")
        config_paths = [root / manifest["client_configs"][m["id"]] for m in manifest["members"]]
        for member, path in zip(manifest["members"], config_paths):
            deployed = json.loads(path.read_text(encoding="utf-8"))
            if (deployed["comm"]["name"] != member["id"]
                    or deployed["tool_agent"]["agent_name"] != member["id"]
                    or Path(deployed["tool_agent"]["tools_config"]).stem != member["role"]):
                parser.error(f"client identity/role differs from bundle: {path}")
    if args.list_pairs:
        for row in pair_catalog(manifest["members"]):
            print(f"{row['pair_id']:2d}  {row['restricted'][0]} + {row['restricted'][1]}  ({row['condition']})")
        return
    config = json.loads(args.window_config.read_text(encoding="utf-8"))
    config = cli_config(config, condition=args.condition, pair_id=args.pair_id)
    try:
        profile_from_config(manifest["members"], config)
    except (ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    # Fail before reset/model cost when any deployed process lacks the new API.
    for url in [*manifest["clients"].values(), args.ioa_server_url]:
        check_window_protocol(url)
    auth = json.loads(args.auth_file.read_text(encoding="utf-8"))
    snapshot = experiment_snapshot(root, scenario_path=scenario_path, client_configs=config_paths)
    report = run_window_episode(ControllerClient(args.sim_url, auth["controller_token"]), manifest["clients"],
        args.ioa_server_url, expected_members=manifest["members"], config=config,
        max_rounds=args.max_rounds, round_timeout=args.round_timeout, expected_scenario=expected_scenario)
    report["experiment_snapshot"] = snapshot
    args.output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = args.output_dir / (report["episode_id"] + ".communication.json")
    trace_path.write_text(json.dumps({"profile": report["profile"], "experiment_rules": report["experiment_rules"],
        "transport": report.pop("transport"),
        "local_events": report.pop("local_events")}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["artifacts"]["communication"] = trace_path.name
    attach_chat_artifacts(report, args.ioa_server_url, args.output_dir)
    report_path = args.output_dir / (report["episode_id"] + ".ioa-run.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("episode_id", "status", "capture_time", "time", "completed_rounds",
          "error", "cleanup_errors", "chat_export_error", "artifacts")}, ensure_ascii=False, indent=2))
    print(f"Report: {report_path}")
    if report["error"] or report["cleanup_errors"] or report["chat_export_error"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
