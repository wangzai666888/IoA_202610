"""Exercise the real ReAct dispatcher with scripted model replies, no paid API.

Load the class AST to avoid importing the Docker-only model/browser dependencies
on Windows. Only models, memory and logging are substituted; the task loop,
dispatch, completion gate, evidence and HTTP simulator are the production code.
"""

import ast
import asyncio
from collections import defaultdict, deque
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
from string import Template
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from simulator.http_client import AgentClient, ControllerClient
from simulator.ioa_tools import bind_simulation_tool_args, tool_schemas
from simulator.service import SimulationHTTPServer, SimulationService
from common.capture_protocol import decode_task, decode_receipt_report


ROOT = Path(__file__).resolve().parents[2]
REACT = ROOT / "im_client/agents/react"
spec = importlib.util.spec_from_file_location("capture_guard_under_test", REACT / "capture_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
EPISODE = "a" * 32
ROUND = EPISODE + ":11"
NAME = "F5_USV_marker"


def tag_receipt(episode=EPISODE, time=11, **extra):
    return json.dumps({"episode_id": episode, "time": time, "accepted": False,
                       "status": "running", **extra})


class Memory:
    def __init__(self): self.messages = []
    def reset(self): self.messages = []
    def add_messages(self, message): self.messages.append(message)
    async def to_messages(self, name): return list(self.messages)


class Log:
    def info(self, *args): pass
    def warn(self, *args): pass
    def log_llm_result(self, *args): pass
    def error(self, *args): pass


class Response:
    def __init__(self, observation): self.observation = observation
    def to_llm_result(self, call_id):
        return SimpleNamespace(content=self.observation, role="tool", tool_call_id=call_id)


def thought():
    return SimpleNamespace(content={"thought": "perform assigned action"},
                           tool_calls=None, parsed_tool_calls=None)


def call(name, **arguments):
    calls = [SimpleNamespace(id="call-id", function=SimpleNamespace(name=name, arguments=arguments))]
    return SimpleNamespace(content=None, tool_calls=calls, parsed_tool_calls=calls)


class Model:
    def __init__(self, replies):
        self.replies = deque(replies)
        self.requests = []
    async def agenerate_response(self, **kwargs):
        self.requests.append(kwargs)
        return self.replies.popleft()


def load_class(path, name, namespace, constants=()):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if (isinstance(node, ast.ClassDef) and node.name == name)
             or (isinstance(node, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id in constants for t in node.targets))]
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


def make_agent(model, mapping=None, max_steps=6, agent_name=NAME, role="marker"):
    namespace = {
        "os": os, "Template": Template, "defaultdict": defaultdict, "deepcopy": deepcopy,
        "ChatHistoryMemory": Memory, "OpenAIChat": lambda **kwargs: model,
        "LLMResult": SimpleNamespace, "ToolResponse": Response, "logger": Log(),
        "TOOL_MAPPING": mapping or {}, "BROWSER_TOOLS_MAPPING": {},
        "CaptureTaskEvidence": guard.CaptureTaskEvidence,
        "decode_task": decode_task,
        "bind_simulation_tool_args": bind_simulation_tool_args,
        "SimpleTextBrowser": lambda **kwargs: None,
        "REACT_SYSTEM_PROMPT": "${name}", "REACT_TASK_PROMPT": "${task_description}",
        "REACT_APPEND_THOUGHT_PROMPT": "thought", "REACT_APPEND_TOOL_PROMPT": "tool",
    }
    cls = load_class(REACT / "react_agent.py", "ReActAgentServer", namespace,
                     constants=("SUBMIT_TOOL", "SUBTASK_TOOL", "CAPTURE_SUBMIT_TOOL"))
    return cls(agent_name, role, "fake-model", tool_schemas(role), max_steps)


class EvidenceTests(unittest.TestCase):
    def test_compact_report_preserves_every_receipt_field_and_bounds_only_model_prose(self):
        evidence = guard.CaptureTaskEvidence("uid", ROUND, NAME)
        evidence.record("sim_try_tag", tag_receipt(reason="r" * 500))
        payload = evidence.finish("MODEL " * 1000)
        text = guard.render_capture_result(payload, "uid", ROUND, NAME)
        facts = json.loads(text.splitlines()[1])
        self.assertEqual(facts["tool_receipts"], payload["tool_receipts"])
        self.assertLess(len(text.split("【模型说明", 1)[1]), 350)
        self.assertEqual(payload["conclusion"], "MODEL " * 1000)

    def test_prose_and_self_state_do_not_count_as_actions(self):
        evidence = guard.CaptureTaskEvidence("uid", ROUND, NAME)
        with self.assertRaisesRegex(ValueError, "no real"):
            evidence.finish("I called sim_try_tag and accepted=false")
        evidence.record("sim_self_state", json.dumps({
            "agent_id": NAME, "episode_id": EPISODE, "time": 11,
        }))
        with self.assertRaisesRegex(ValueError, "no real"):
            evidence.finish("state queried")

    def test_false_tag_is_valid_and_receipts_precede_untrusted_prose(self):
        evidence = guard.CaptureTaskEvidence("uid", ROUND, NAME)
        evidence.record("sim_try_tag", tag_receipt())
        payload = evidence.finish("MODEL CLAIM: accepted=true")
        text = guard.render_capture_result(payload, "uid", ROUND, NAME)
        self.assertLess(text.index('"accepted": false'), text.index("MODEL CLAIM"))
        for field, bad in [("uid", "old-task"), ("sim_round_id", EPISODE + ":10"),
                           ("agent_name", "F5_UAV_marker")]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "another"):
                guard.render_capture_result({**payload, field: bad}, "uid", ROUND, NAME)

    def test_invalid_or_old_receipts_cannot_unlock_submit(self):
        for raw in ['{"error":"HTTP 409"}', tag_receipt(time=10),
                    tag_receipt(episode="b" * 32), tag_receipt(accepted="false")]:
            with self.subTest(raw=raw):
                evidence = guard.CaptureTaskEvidence("uid", ROUND, NAME)
                with self.assertRaises(ValueError): evidence.record("sim_try_tag", raw)
                self.assertFalse(evidence.has_action)

    def test_already_captured_and_unseen_observation_are_valid_receipts(self):
        evidence = guard.CaptureTaskEvidence("uid", ROUND, NAME)
        evidence.record("sim_try_tag", tag_receipt(status="captured", reason="already_captured"))
        self.assertTrue(evidence.has_action)
        evidence = guard.CaptureTaskEvidence("observer-task", ROUND, "observer")
        evidence.record("sim_observe", json.dumps({
            "observer_id": "observer", "episode_id": EPISODE, "time": 11,
            "observed_at": 10, "target_position": None,
        }))
        self.assertTrue(evidence.has_action)


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SIM_URL": "http://fake-simulator"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_premature_submit_is_rejected_then_real_tag_can_complete(self):
        calls = []
        def tag(**kwargs):
            calls.append(kwargs)
            return tag_receipt()
        model = Model([call("submit_task", conclusion="already did it"),
                       call("sim_try_tag"),
                       call("submit_task", conclusion="actual receipt received")])
        agent = make_agent(model, {"sim_try_tag": tag})
        result = asyncio.run(agent.run("只调用一次 sim_try_tag", "uid", ROUND))
        self.assertEqual(calls, [{"episode_id": EPISODE, "expected_time": 11}])
        self.assertEqual(len(result["tool_receipts"]), 1)
        self.assertFalse(result["tool_receipts"][0]["result"]["accepted"])
        self.assertTrue(any("submit_task rejected" in str(m.content)
                            for m in agent.memory["uid"].messages))
        self.assertEqual(len(model.requests), 3)
        self.assertTrue(all("response_format" not in req for req in model.requests))
        self.assertNotIn("uid", agent.capture_evidence_by_uid)

    def test_direct_tool_call_is_executed_once_with_two_model_requests_total(self):
        calls = []
        def tag(**kwargs):
            calls.append(kwargs)
            return tag_receipt()
        model = Model([call("sim_try_tag"), call("submit_task", conclusion="done")])
        agent = make_agent(model, {"sim_try_tag": tag})
        result = asyncio.run(agent.run("tag", "uid", ROUND))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(any(getattr(m, "role", None) == "tool"
                            for m in model.requests[1]["history"]))
        self.assertEqual(result["tool_receipts"][0]["tool"], "sim_try_tag")

    def test_exhausted_prose_only_task_fails_instead_of_completing(self):
        model = Model([call("submit_task", conclusion="imagined receipt")])
        agent = make_agent(model, max_steps=0)
        with self.assertRaisesRegex(ValueError, "no real"):
            asyncio.run(agent.run("tag", "uid", ROUND))
        # No forced submit on the last step when no action has occurred.
        self.assertIsNone(model.requests[-1]["tool_choice"])
        self.assertEqual(agent.conclusion["uid"], "")

    def test_error_and_previous_task_cannot_supply_receipts(self):
        def fail(**kwargs): raise RuntimeError("HTTP 409: stale episode_id")
        model = Model([call("sim_try_tag"),
                       call("submit_task", conclusion="I called the tool")])
        agent = make_agent(model, {"sim_try_tag": fail}, max_steps=1)
        with self.assertRaisesRegex(ValueError, "no real"):
            asyncio.run(agent.run("tag", "uid", ROUND))
        asyncio.run(agent.create_task("tag", "old", ROUND))
        agent.capture_evidence_by_uid["old"].record("sim_try_tag", tag_receipt())
        asyncio.run(agent.create_task("tag", "new", EPISODE + ":12"))
        self.assertFalse(agent.capture_evidence_by_uid["new"].has_action)
        asyncio.run(agent.create_task("tag", "old", EPISODE + ":12"))
        self.assertFalse(agent.capture_evidence_by_uid["old"].has_action)

    def test_non_capture_task_can_still_submit_prose(self):
        with patch.dict(os.environ, {"SIM_URL": ""}):
            model = Model([thought(), call("submit_task", conclusion="ordinary answer")])
            agent = make_agent(model)
            self.assertEqual(asyncio.run(agent.run("ordinary task", "uid")), "ordinary answer")
            self.assertEqual(len(model.requests), 2)

    def test_text_only_reply_does_not_execute_or_complete_and_next_call_can_recover(self):
        model = Model([thought(), call("sim_try_tag"), call("submit_task", conclusion="done")])
        calls = []
        def tag(**kwargs):
            calls.append(kwargs)
            return tag_receipt()
        result = asyncio.run(make_agent(model, {"sim_try_tag": tag}).run("tag", "uid", ROUND))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(len(result["tool_receipts"]), 1)

    def test_role_tool_restriction_is_kept_in_single_request_loop(self):
        model = Model([call("sim_observe"), call("sim_try_tag"),
                       call("submit_task", conclusion="done")])
        def forbidden(): raise AssertionError("marker must not execute observer tool")
        result = asyncio.run(make_agent(model, {
            "sim_observe": forbidden, "sim_try_tag": lambda **kwargs: tag_receipt(),
        }).run("tag", "uid", ROUND))
        self.assertEqual([r["tool"] for r in result["tool_receipts"]], ["sim_try_tag"])

    def test_multiple_actions_in_one_reply_use_the_same_receipt_gate(self):
        combined = call("sim_self_state")
        combined.parsed_tool_calls.extend(call("sim_try_tag").parsed_tool_calls)
        model = Model([combined, call("submit_task", conclusion="done")])
        result = asyncio.run(make_agent(model, {
            "sim_self_state": lambda: json.dumps({"agent_id": NAME, "episode_id": EPISODE, "time": 11}),
            "sim_try_tag": lambda **kwargs: tag_receipt(),
        }).run("read own state then tag", "uid", ROUND))
        self.assertEqual([r["tool"] for r in result["tool_receipts"]], ["sim_self_state", "sim_try_tag"])
        self.assertEqual(len(model.requests), 2)

    def test_t11_missing_usv_tag_recovered_against_real_simulator_without_stepping(self):
        ids = ["F5_UAV_observer", "F5_USV_observer", "F5_UUV_leader", NAME, "F5_UAV_marker"]
        auth = {"controller_token": "test-controller-token-000",
                "agent_tokens": {name: "test-" + name + "-token-000" for name in ids}}
        with tempfile.TemporaryDirectory() as output:
            service = SimulationService(ROOT / "simulator/scenarios/five_team.json", Path(output), auth)
            server = SimulationHTTPServer(("127.0.0.1", 0), service)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                controller = ControllerClient(url, auth["controller_token"])
                client = AgentClient(url, auth["agent_tokens"][NAME])
                state = controller.reset()
                episode = state["episode_id"]
                for time in range(11): controller.step(episode, time)
                model = Model([call("submit_task", conclusion="fake accepted=false"),
                               call("sim_try_tag"),
                               call("submit_task", conclusion="done")])
                agent = make_agent(model, {"sim_try_tag": lambda episode_id, expected_time:
                                           json.dumps(client.try_tag(episode_id, expected_time))})
                result = asyncio.run(agent.run("只调用一次 sim_try_tag", "real-http-task", episode + ":11"))
                self.assertEqual(controller.status()["time"], 11)
                self.assertEqual([{k: v for k, v in c.items() if k != "reason"} for c in controller.audit()["calls"]], [
                    {"agent_id": NAME, "time": 11, "operation": "tag", "accepted": False},
                ])
                text = guard.render_capture_result(result, "real-http-task", episode + ":11", NAME)
                self.assertIn('"accepted": false', text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class AdapterTests(unittest.TestCase):
    def run_adapter(self, response):
        class Data:
            def __init__(self, **data): self.data = data
            def model_dump(self): return self.data
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs): return response
        namespace = {
            "os": os, "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="uid")),
            "AgentAdapter": object, "Container": object, "TaskDesc": Data,
            "LLMResult": SimpleNamespace, "logger": Log(),
            "httpx": SimpleNamespace(AsyncClient=Client, Timeout=lambda value: value),
            "render_capture_result": guard.render_capture_result,
            "decode_task": decode_task, "decode_receipt_report": decode_receipt_report,
        }
        cls = load_class(REACT / "react_agent_adapter.py", "ReActAgent", namespace)
        adapter = cls.__new__(cls)
        adapter.url, adapter.agent_name = "http://tool-backend", NAME
        with patch.dict(os.environ, {"SIM_URL": "http://fake"}):
            return asyncio.run(adapter.run("tag", ROUND))

    def test_http_error_is_not_returned_as_successful_task_text(self):
        def raise_error(): raise RuntimeError("HTTP 500")
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            self.run_adapter(SimpleNamespace(raise_for_status=raise_error, text="Internal Server Error"))

    def test_old_backend_without_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "update the tool image"):
            self.run_adapter(SimpleNamespace(raise_for_status=lambda: None,
                                             json=lambda: "I claim I tagged"))

    def test_verified_response_is_returned_with_original_false_receipt(self):
        evidence = guard.CaptureTaskEvidence("uid", ROUND, NAME)
        evidence.record("sim_try_tag", tag_receipt())
        result = self.run_adapter(SimpleNamespace(raise_for_status=lambda: None,
                                                  json=lambda: evidence.finish("done")))
        self.assertIn('"accepted": false', result)


if __name__ == "__main__":
    unittest.main()
