"""Exercise actual coordinator decisions, executor guards and ordered broadcasts.

Only model replies, persistence and socket transport are substituted. All tool
actions cross the real simulator HTTP boundary; no paid model API is used.
"""
import ast
import asyncio
from collections import defaultdict
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from common.capture_protocol import BATCH_PREFIX, TASK_PREFIX, decode_task, decode_receipt_report
from simulator.http_client import AgentClient, ControllerClient
from simulator.ioa_run import round_goal, verify_round_audit
from simulator.service import SimulationHTTPServer, SimulationService
from simulator.tests.capture_helpers import load_helper
from simulator.tests.test_capture_evidence import Model, call, guard, make_agent
from simulator.tests.test_capture_performance import load_runtime, Memory, Message, rounds


ROOT = Path(__file__).resolve().parents[2]
phases = load_helper("capture_state")
NAMES = ["F5_UAV_observer", "F5_USV_observer", "F5_UUV_leader", "F5_USV_marker", "F5_UAV_marker"]
ROLES = dict(zip(NAMES, ["observer", "observer", "leader", "marker", "marker"]))
MEMBERS = [{"id": name, "role": role} for name, role in ROLES.items()]
EPISODE = "a" * 32


def new_state(second=0, previous=None, episode=EPISODE):
    return phases.begin_capture_round(round_goal(episode, second, 5, MEMBERS), f"{episode}:{second}", previous)


def receipt(contract, **extra):
    episode, second = contract["round_id"].split(":")
    result = {"episode_id": episode, "time": int(second)}
    if contract["tool"] == "sim_observe":
        result.update(observer_id=contract["agent_id"], observed_at=int(second), target_position=[0, 40])
    elif contract["tool"] == "sim_move_to":
        result.update(accepted=True, waypoint=[contract["arguments"][k] for k in ("x", "y")])
    else:
        result.update(accepted=False, status="running")
    result.update(extra)
    evidence = guard.CaptureTaskEvidence("task-uid", contract["round_id"], contract["agent_id"], contract)
    evidence.record(contract["tool"], json.dumps(result))
    return guard.render_capture_result(evidence.finish("完成"), "task-uid", contract["round_id"], contract["agent_id"])


def assign(state, phase, names):
    tool = {"observe": "sim_observe", "waypoint": "sim_move_to", "tag": "sim_try_tag"}[phase]
    actions = [{"agent_id": name, "tool": tool, "arguments": {}} for name in names]
    if phase == "waypoint":
        for action in actions:
            action.update(arguments={"x": 0, "y": 40}, observed_at=0)
    batch = phases.make_batch(state, phase, actions)
    phases.accept_batch(state, batch, NAMES[2], names)
    return batch


def complete(state, batch):
    for task in batch["tasks"]:
        phases.record_result(state, task["agent_id"], receipt(task))


class PhaseContractTests(unittest.TestCase):
    def test_original_mixed_batch_and_missing_observers_rejected_without_partial_assignment(self):
        state = new_state()
        before = deepcopy(state)
        actions = [{"agent_id": n, "tool": "sim_observe" if i < 2 else "sim_move_to",
                    "arguments": {} if i < 2 else {"x": 0, "y": 0}, **({} if i < 2 else {"observed_at": 0})}
                   for i, n in enumerate(NAMES)]
        with self.assertRaises(ValueError):
            phases.accept_batch(state, phases.make_batch(state, "observe", actions), NAMES[2], NAMES)
        self.assertEqual(state, before)
        complete(state, assign(state, "observe", NAMES[:2]))
        before = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "every member"):
            assign(state, "waypoint", NAMES[2:])
        with self.assertRaisesRegex(ValueError, "waypoint receipts missing"):
            assign(state, "tag", NAMES[3:])
        self.assertFalse(phases.ready_to_close(state))
        self.assertEqual(state, before)

    def test_delayed_duplicate_cannot_complete_the_next_action_for_same_observer(self):
        state = new_state()
        observation = assign(state, "observe", NAMES[:2])
        complete(state, observation)
        batch = assign(state, "waypoint", NAMES)
        before = deepcopy(state)
        self.assertFalse(phases.record_result(state, NAMES[0], receipt(observation["tasks"][0])))
        self.assertEqual(state, before)
        self.assertFalse(phases.accept_batch(state, batch, NAMES[2], NAMES))
        self.assertEqual(state, before)

    def test_real_observation_does_not_fulfil_waypoint_contract_and_old_receipt_is_rejected(self):
        state = new_state()
        complete(state, assign(state, "observe", NAMES[:2]))
        task = assign(state, "waypoint", NAMES)["tasks"][0]
        evidence = guard.CaptureTaskEvidence("uid", task["round_id"], task["agent_id"], task)
        with self.assertRaisesRegex(ValueError, "requires sim_move_to"):
            evidence.record("sim_observe", json.dumps({"episode_id": EPISODE, "time": 0}))
        report = receipt(task)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            decode_receipt_report(report, {**task, "id": "different-task"})
        self.assertFalse(evidence.has_action)

    def test_only_completed_rounds_advance_and_new_episode_has_no_prior_destinations(self):
        state = new_state()
        with self.assertRaisesRegex(ValueError, "cannot advance"):
            new_state(1, state)
        complete(state, assign(state, "observe", NAMES[:2]))
        complete(state, assign(state, "waypoint", NAMES))
        complete(state, assign(state, "tag", NAMES[3:]))
        self.assertTrue(phases.ready_to_close(state))
        following = new_state(1, state)
        self.assertEqual(following["waypoints"], state["waypoints"])
        self.assertEqual(following["pending"], {})
        self.assertEqual(following["completed"], {})
        new = new_state(previous=state, episode="b" * 32)
        self.assertEqual(new["waypoints"], {})
        self.assertEqual(new["observations"], {})
        self.assertEqual(phases.next_phase(new), "observe")

    def test_no_model_side_effects_for_invalid_plans_and_bounded_retries(self):
        state = new_state()
        complete(state, assign(state, "observe", NAMES[:2]))
        before = deepcopy(state)
        wrong = {"waypoints": [{"agent_id": n, "x": 0, "y": 0, "observed_at": 0} for n in NAMES]}
        model = Model([Message(content=wrong)] * 3)
        with self.assertRaisesRegex(ValueError, "bounded corrections"):
            asyncio.run(phases.plan_waypoints(model, state))
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(state, before)
        self.assertIn("未执行", model.requests[1]["append_prompt"])

    def test_wrong_tool_wrong_arguments_and_duplicate_action_are_blocked_before_http(self):
        contract = {"id": "contract", "round_id": EPISODE + ":0", "agent_id": NAMES[0],
                    "tool": "sim_move_to", "arguments": {"x": 0, "y": 40}, "observed_at": 0}
        calls = []
        def move(**kwargs):
            calls.append(kwargs)
            return json.dumps({"episode_id": EPISODE, "time": 0, "accepted": True, "waypoint": [0, 40]})
        def observe():
            self.fail("wrong tool reached simulator")
        model = Model([call("sim_observe"), call("sim_move_to", x=0, y=0),
                       call("sim_move_to", x=0, y=40), call("sim_move_to", x=0, y=40),
                       call("submit_task", conclusion="完成")])
        with patch.dict(os.environ, {"SIM_URL": "http://fake"}):
            agent = make_agent(model, {"sim_move_to": move, "sim_observe": observe},
                               agent_name=NAMES[0], role="observer")
            result = asyncio.run(agent.run(TASK_PREFIX + json.dumps(contract), "uid", contract["round_id"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["contract"], contract)
        self.assertEqual(len(result["tool_receipts"]), 1)
        self.assertEqual(model.requests[-1]["tool_choice"]["function"]["name"], "submit_task")


class CopyBank(dict):
    """Persistence reads return snapshots, like the production database bank."""
    def __getitem__(self, key): return deepcopy(super().__getitem__(key))
    def __setitem__(self, key, value): super().__setitem__(key, deepcopy(value))


def info_factory(**kwargs):
    return SimpleNamespace(**(dict(sim_round_id=None, sim_round_message_starts=[],
                                  sim_round_error=None, sim_capture_state=None, conclusion=None) | kwargs))


def coordinator_runtime():
    methods = {"coordination", "_coordination_discussion", "_handle_info_message", "_generate_response",
               "_generate_capture_response", "_update_memory_and_task_manager", "_send_message"}
    namespace = {name: getattr(phases, name) for name in (
        "accept_batch", "decode_batch", "record_result", "ready_to_close", "role_members", "next_phase",
        "plan_waypoints", "missing_observers", "make_batch")}
    namespace.update(BATCH_PREFIX=BATCH_PREFIX, begin_round=rounds.begin_round,
                     parse_round_id=rounds.parse_round_id, round_message_count=rounds.round_message_count,
                     CommunicationInfo=info_factory, ChatHistoryMemory=Memory)
    return load_runtime(methods, namespace)


def broadcast_runtime(send):
    tree = ast.parse((ROOT / "im_server/app.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "broadcast_session_message")
    async def frontend(*args): pass
    namespace = dict(json=json, session_broadcast_locks=defaultdict(asyncio.Lock),
                     chat_record_manager=CopyBank(), session_manager=SimpleNamespace(sessions={}),
                     connection_manager=SimpleNamespace(send_personal_message=send), send_to_frontend=frontend)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "broadcast", "exec"), namespace)
    return namespace


class BroadcastTests(unittest.TestCase):
    def test_entire_broadcast_is_ordered_even_when_next_phase_arrives_early(self):
        async def exercise():
            reached_last, release = asyncio.Event(), asyncio.Event()
            delivered = defaultdict(list)
            async def send(name, msg):
                if name == "last" and msg.content == "receipt":
                    reached_last.set()
                    await release.wait()
                delivered[name].append(msg.content)
            runtime = broadcast_runtime(send)
            runtime["chat_record_manager"]["s"] = {"chat_record": []}
            runtime["session_manager"].sessions["s"] = ["leader", "last"]
            fn = runtime["broadcast_session_message"]
            first = asyncio.create_task(fn(Message(comm_id="s", content="receipt")))
            await reached_last.wait()
            second = asyncio.create_task(fn(Message(comm_id="s", content="next_phase")))
            await asyncio.sleep(0)
            self.assertEqual(delivered["leader"], ["receipt"])
            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(dict(delivered), {n: ["receipt", "next_phase"] for n in ["leader", "last"]})
            self.assertEqual([m.content for m in runtime["chat_record_manager"]["s"]["chat_record"]],
                             ["receipt", "next_phase"])
        asyncio.run(exercise())


class FullEpisodeTests(unittest.TestCase):
    def test_three_full_episodes_reuse_clients_and_tools_with_real_http_audits(self):
        auth = {"controller_token": "test-controller-token",
                "agent_tokens": {name: "test-token-" + name for name in NAMES}}
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ, {"SIM_URL": "http://test"}):
            service = SimulationService(ROOT / "simulator/scenarios/five_team.json", Path(output), auth)
            server = SimulationHTTPServer(("127.0.0.1", 0), service)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                controller = ControllerClient(url, auth["controller_token"])
                asyncio.run(self.run_episodes(controller, url, auth))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    async def run_episodes(self, controller, url, auth):
        runtime = coordinator_runtime()
        layers, queues, errors = {}, {n: asyncio.Queue() for n in NAMES}, []
        async def deliver(name, message):
            await queues[name].put(Message.model_validate_json(message.model_dump_json()))
            await asyncio.sleep(0)  # Allow leader responses during a broadcast.
        server = broadcast_runtime(deliver)

        class Planner:
            def __init__(self): self.requests = []; self.invalid = 0
            async def agenerate_response(self, **kwargs):
                self.requests.append(kwargs)
                context, _ = json.JSONDecoder().raw_decode(kwargs["append_prompt"])
                if context["time"] == 0 and "未执行" not in kwargs["append_prompt"]:
                    self.invalid += 1
                    # The actual failure: a plan that omits the two observers.
                    members = NAMES[2:]
                else:
                    members = NAMES
                sample = max(context["observations"].values(), key=lambda s: s["observed_at"])
                return Message(content={"waypoints": [{"agent_id": n, "x": sample["target_position"][0],
                    "y": sample["target_position"][1], "observed_at": sample["observed_at"]} for n in members]})

        class Backend:
            def __init__(self, agent, model, index): self.agent, self.model, self.index = agent, model, index
            async def run(self, task_desc, sim_round_id):
                task = decode_task(task_desc, sim_round_id, self.agent.agent_name)
                await asyncio.sleep(self.index * 0.0001)  # Different task completion order.
                self.model.replies.extend([call(task["tool"], **task["arguments"]), call("submit_task", conclusion="完成")])
                uid = uuid.uuid4().hex
                result = await self.agent.run(task_desc, uid, sim_round_id)
                report = guard.render_capture_result(result, uid, sim_round_id, self.agent.agent_name)
                decode_receipt_report(report, task)
                return report

        planner = Planner()
        for index, name in enumerate(NAMES):
            client = AgentClient(url, auth["agent_tokens"][name])
            model = Model([])
            mapping = {"sim_observe": lambda c=client: json.dumps(c.observe()),
                       "sim_move_to": lambda episode_id, expected_time, x, y, c=client:
                           json.dumps(c.set_waypoint(episode_id, expected_time, [x, y])),
                       "sim_try_tag": lambda episode_id, expected_time, c=client:
                           json.dumps(c.try_tag(episode_id, expected_time))}
            agent = make_agent(model, mapping, agent_name=name, role=ROLES[name])
            layer = runtime["CommunicationLayer"]()
            layer.name, layer.tool_agent, layer.support_nested_teams = name, Backend(agent, model, index), False
            layer.observation_func = lambda **kwargs: ""
            layer.comm_bank, layer.task_manager_bank = CopyBank(), CopyBank()
            layer.llm = planner if ROLES[name] == "leader" else Model([])
            async def send_wire(data):
                await server["broadcast_session_message"](Message.model_validate_json(data))
            layer.server_websocket = SimpleNamespace(send_message=send_wire)
            layers[name] = layer

        async def worker(name):
            while True:
                message = await queues[name].get()
                try:
                    await layers[name].coordination(message, message.comm_id, max_turns=message.max_turns)
                except Exception as exc:
                    errors.append((name, message.type, repr(exc)))
                finally:
                    queues[name].task_done()
        workers = [asyncio.create_task(worker(n)) for n in NAMES]
        episodes = []
        try:
            for _ in range(3):
                state = controller.reset()
                episode, comm = state["episode_id"], uuid.uuid4().hex
                episodes.append(episode)
                server["session_manager"].sessions[comm] = [NAMES[2], *NAMES[:2], *NAMES[3:]]
                server["chat_record_manager"][comm] = {"chat_record": []}
                leader = layers[NAMES[2]]
                leader.comm_bank[comm] = info_factory(comm_id=comm, memory=Memory(), team_members=[{"name": n} for n in NAMES],
                    team_up_depth=0, is_collaborative_planning_enabled=False)
                leader.task_manager_bank[comm] = runtime["TaskManager"](comm)
                for second in range(120):
                    key = f"{episode}:{second}"
                    info = leader.comm_bank[comm]
                    rounds.begin_round(info, leader.task_manager_bank[comm], key, 24,
                                       round_goal(episode, second, 5, MEMBERS))
                    leader.comm_bank[comm] = info
                    await leader.coordination(None, comm)
                    async def closed():
                        while True:
                            if errors: raise AssertionError(errors)
                            if all(comm in l.comm_bank and l.comm_bank[comm].sim_round_id == key
                                   and l.comm_bank[comm].conclusion is not None for l in layers.values()):
                                return
                            await asyncio.sleep(0.001)
                    await asyncio.wait_for(closed(), timeout=10)
                    await asyncio.gather(*(q.join() for q in queues.values()))
                    self.assertEqual(errors, [])
                    for layer in layers.values():
                        info = layer.comm_bank[comm]
                        snapshot = rounds.round_status(info, layer.task_manager_bank[comm])
                        self.assertEqual(snapshot["pending_tasks"], [])
                        self.assertEqual(snapshot["failed_tasks"], [])
                        self.assertEqual(snapshot["await_agents"], [])
                        self.assertTrue(phases.ready_to_close(info.sim_capture_state))
                    audit = controller.audit()
                    self.assertEqual(verify_round_audit(audit, episode, second, 5, state["members"]), [])
                    calls = [c for c in audit["calls"] if c["time"] == second]
                    if second == 0:
                        self.assertEqual({c["agent_id"] for c in calls if c["operation"] == "waypoint"}, set(NAMES))
                        self.assertEqual(len(calls), 9)
                    if second % 5:
                        self.assertTrue(all(c["operation"] == "tag" for c in calls))
                    state = controller.status()
                    if state["status"] != "running": break
                    controller.step(episode, second)
                self.assertEqual(controller.status()["status"], "captured")
                self.assertEqual(controller.status()["capture_time"], 18)
            self.assertEqual(len(set(episodes)), 3)
            self.assertEqual(planner.invalid, 3)
            # Four sampling rounds per episode, plus one correction per t=0.
            self.assertEqual(len(planner.requests), 15)
            for layer in layers.values():
                self.assertEqual(len(layer.comm_bank), 3)
        finally:
            for worker_task in workers: worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
