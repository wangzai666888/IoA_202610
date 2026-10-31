"""Run production communication/task/ReAct code with a scripted model.

AST loading avoids optional Docker/Milvus imports; no live models are called.
The five-agent check crosses the real simulator HTTP boundary and audit.
"""

import ast
import asyncio
from enum import Enum
import json
import os
from pathlib import Path
from string import Template
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from simulator.http_client import AgentClient, ControllerClient
from simulator.ioa_run import verify_round_audit
from simulator.service import SimulationHTTPServer, SimulationService
from simulator.tests.test_capture_evidence import Model, Log, call, guard, make_agent
from simulator.tests.capture_helpers import load_helper


ROOT = Path(__file__).resolve().parents[2]
COMM = ROOT / "im_client/communication"


capture = load_helper("capture_execution")
rounds = load_helper("sim_round_state")


class Message(SimpleNamespace):
    __hash__ = object.__hash__
    def __init__(self, **kwargs):
        defaults = dict(next_speaker="", proposal_id="", updated_plan="", triggers=[],
                        team_members=[], team_up_depth=0, is_collaborative_planning_enabled=False,
                        max_turns=24)
        super().__init__(**(defaults | kwargs))
    def to_llm_result(self): return Message(content=self.content, name=self.sender, role="assistant")
    def model_dump_json(self): return json.dumps(vars(self))
    @classmethod
    def model_validate_json(cls, value): return cls(**json.loads(value))


class Memory:
    def __init__(self): self.messages = []
    def add_messages(self, message): self.messages.append(message)
    async def to_messages(self, **kwargs): return [{"role": "assistant", "content": "history"}]


def load_runtime(extra_methods=(), extra_namespace=None):
    namespace = {
        "os": os, "time": time, "uuid": uuid, "Enum": Enum, "json": json,
        "Template": Template, "logger": Log(), "Serializable": object,
        "LLMResult": Message, "AgentMessage": Message,
        "CommunicationState": SimpleNamespace(DISCUSSION="discussion"),
        "CommunicationType": SimpleNamespace(INFORM_TASK_RESULT="result", CONCLUSION="conclusion",
            DISCUSSION="discussion", INFORM_TASK_PROGRESS="progress", PAUSE="pause",
            CONCLUDE_GROUP_DISCUSSION="conclude", SYNC_TASK_ASSIGNMENT="sync", ASYNC_TASK_ASSIGNMENT="async"),
        "capture_assignment": capture.capture_assignment,
        "capture_round_conclusion": capture.capture_round_conclusion,
        "snapshot_sim_round": rounds.round_status,
        "_capture_prompt_start": lambda info: 0,
        "COMM_CONCLUDE_APPEND_PROMPT": "$goal",
        "EXEC_TASK_CONCLUSION_APPEND_PROMPT": "$task_desc\n$task_conclusion",
    }
    namespace.update(extra_namespace or {})
    task_tree = ast.parse((COMM / "task_management.py").read_text())
    tree = ast.parse((COMM / "communication_layer.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CommunicationLayer")
    selected = {
        "_handle_sync_task_assignment", "_rephrase_task_to_tool_agent",
        "_call_tool_agent_and_inform", "tool_agent_run", "_inform_task_result",
        "_conclude_group_discussion",
    }
    selected.update(extra_methods)
    cls.body = [n for n in cls.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in selected]
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    task_classes = {"TaskStatus", "TaskAssignmentRespondManager", "TaskEntry", "TaskManager"}
    nodes.extend(n for n in task_tree.body if isinstance(n, ast.ClassDef) and n.name in task_classes)
    nodes.append(cls)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(COMM), "exec"), namespace)
    return namespace


def make_layer(name, round_id, backend=None):
    runtime = load_runtime()
    layer = runtime["CommunicationLayer"]()
    layer.name, layer.tool_agent, layer.support_nested_teams = name, backend, False
    layer.observation_func = lambda **kwargs: ""
    layer.llm = Model([])  # Any unexpected communication model request fails.
    info = SimpleNamespace(comm_id="session", memory=Memory(), team_up_depth=0,
                           sim_round_message_starts=[], sim_round_error=None)
    manager = runtime["TaskManager"]("session")
    rounds.begin_round(info, manager, round_id, 24, "goal")
    layer.comm_bank, layer.task_manager_bank = {"session": info}, {"session": manager}
    layer.sent = []
    async def send(message): layer.sent.append(message)
    layer._send_message = send
    return layer


class CaptureHandoffTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"SIM_URL": "http://test"})
        env.start()
        self.addCleanup(env.stop)
        self.key = "a" * 32 + ":1"

    def test_delivered_assignment_preserves_parameters_without_model_or_old_history(self):
        layer = make_layer("marker", self.key)
        layer.comm_bank["session"].memory.messages.append(Message(content="STALE waypoint 99,99"))
        message = Message(proposal_id=self.key, next_speaker=["marker"], sender="leader",
                          content="marker 本次 sim_move_to x=5.25 y=40 observed_at=0")
        result = asyncio.run(layer._rephrase_task_to_tool_agent("session", assignment=message))
        self.assertIn(message.content, result[1])
        self.assertNotIn("STALE", result[1])
        self.assertEqual(layer.llm.requests, [])
        self.assertIsNone(result[0])

    def test_missing_foreign_or_misdirected_assignment_is_rejected(self):
        layer = make_layer("marker", self.key)
        for message in [None,
                        Message(proposal_id="b" * 32 + ":1", next_speaker=["marker"], content="tag"),
                        Message(proposal_id=self.key, next_speaker=["observer"], content="observe"),
                        Message(proposal_id=self.key, next_speaker=["marker"], content="")]:
            with self.subTest(message=message), self.assertRaises(ValueError):
                asyncio.run(layer._rephrase_task_to_tool_agent("session", assignment=message))
        self.assertEqual(layer.llm.requests, [])

    def test_short_conclusion_does_not_hide_unfinished_tasks_or_assert_capture(self):
        layer = make_layer("leader", self.key)
        manager = layer.task_manager_bank["session"]
        manager.create_task("tag", "tag", "marker")
        manager.task_assign_manager.register_await_agents(["marker"])
        asyncio.run(layer._conclude_group_discussion("session"))
        summary = json.loads(layer.comm_bank["session"].conclusion)
        self.assertEqual(len(summary["pending_tasks"]), 1)
        self.assertEqual(summary["await_agents"], ["marker"])
        self.assertEqual(summary["audit"], "pending_controller_verification")
        self.assertNotIn("captured", summary)
        self.assertEqual(layer.llm.requests, [])
        self.assertEqual(layer.sent[-1].type, "conclusion")

    def test_non_capture_summary_still_calls_model(self):
        layer = make_layer("leader", self.key)
        layer.llm = Model([Message(content="normal answer")])
        with patch.dict(os.environ, {"SIM_URL": ""}):
            asyncio.run(layer._conclude_group_discussion("session"))
        self.assertEqual(layer.comm_bank["session"].conclusion, "normal answer")
        self.assertEqual(len(layer.llm.requests), 1)

    def test_five_agent_handoff_audits_six_rounds_and_updates_waypoints_on_new_sample(self):
        names = ["F5_UAV_observer", "F5_USV_observer", "F5_UUV_leader", "F5_USV_marker", "F5_UAV_marker"]
        roles = dict(zip(names, ["observer", "observer", "leader", "marker", "marker"]))
        auth = {"controller_token": "test-controller-token",
                "agent_tokens": {name: "test-token-" + name for name in names}}
        with tempfile.TemporaryDirectory() as output:
            service = SimulationService(ROOT / "simulator/scenarios/five_team.json", Path(output), auth)
            server = SimulationHTTPServer(("127.0.0.1", 0), service)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                controller = ControllerClient(url, auth["controller_token"])
                state = controller.reset()
                episode = state["episode_id"]
                layers, models = {}, {}
                class Backend:
                    def __init__(self, agent): self.agent = agent
                    async def run(self, task_desc, sim_round_id):
                        uid = uuid.uuid4().hex
                        payload = await self.agent.run(task_desc, uid, sim_round_id)
                        return guard.render_capture_result(payload, uid, sim_round_id, self.agent.agent_name)
                for name in names:
                    client = AgentClient(url, auth["agent_tokens"][name])
                    model = Model([])
                    mapping = {
                        "sim_observe": lambda c=client: json.dumps(c.observe()),
                        "sim_move_to": lambda episode_id, expected_time, x, y, c=client:
                            json.dumps(c.set_waypoint(episode_id, expected_time, [x, y])),
                        "sim_try_tag": lambda episode_id, expected_time, c=client:
                            json.dumps(c.try_tag(episode_id, expected_time)),
                    }
                    agent = make_agent(model, mapping, agent_name=name, role=roles[name])
                    layers[name] = make_layer(name, episode + ":0", Backend(agent))
                    models[name] = model

                async def run_batches():
                    for second in range(6):
                        key = episode + f":{second}"
                        if second:
                            for layer in layers.values():
                                rounds.begin_round(layer.comm_bank["session"], layer.task_manager_bank["session"],
                                                   key, 24, "continue existing waypoint")
                        batches = []
                        if second % 5 == 0:
                            batches.extend([(names[:2], "sim_observe", {}),
                                            (names, "sim_move_to", {"x": second, "y": 40})])
                        batches.append((names[3:], "sim_try_tag", {}))
                        for receivers, tool, args in batches:
                            content = (f"本批 {','.join(receivers)} 各自调用 {tool} {json.dumps(args)} "
                                       f"observed_at={second - second % 5}")
                            for name in receivers:
                                models[name].replies.extend([call(tool, **args), call("submit_task", conclusion="完成")])
                            await asyncio.gather(*[
                                layers[name]._handle_sync_task_assignment(Message(
                                    proposal_id=key, next_speaker=receivers, sender=names[2], content=content,
                                ), "session") for name in receivers
                            ])
                        self.assertEqual(verify_round_audit(controller.audit(), episode, second, 5, state["members"]), [])
                        for layer in layers.values():
                            await layer._conclude_group_discussion("session")
                            status = rounds.round_status(layer.comm_bank["session"], layer.task_manager_bank["session"])
                            self.assertTrue(status["concluded"])
                            self.assertEqual(status["pending_tasks"], [])
                            self.assertEqual(status["failed_tasks"], [])
                            self.assertEqual(layer.llm.requests, [])
                        controller.step(episode, second)
                asyncio.run(run_batches())
                # 9 tasks at t=0 and t=5; only 2 tags at t=1..4.
                self.assertEqual(sum(len(m.requests) for m in models.values()), 52)
                audit = controller.audit()["calls"]
                self.assertEqual(sum(c["operation"] == "waypoint" for c in audit), 10)
                self.assertEqual(sum(c["operation"] == "tag" for c in audit), 12)
                self.assertEqual(sum(c["operation"] == "observe" for c in audit), 4)
                waypoints = [c for c in audit if c["operation"] == "waypoint"]
                self.assertEqual({c["time"] for c in waypoints}, {0, 5})
                self.assertTrue(all(c["point"] == [5, 40] for c in waypoints if c["time"] == 5))
                self.assertEqual(controller.status()["time"], 6)
                for layer in layers.values():
                    for message in layer.sent:
                        if message.type == "result":
                            self.assertIn("tool_receipts", message.content)
                            self.assertNotIn("本批", message.content)
                            self.assertIn("本批", message.task_desc)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
