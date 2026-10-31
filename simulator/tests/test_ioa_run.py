"""Controller integration checks with the real simulator HTTP boundary and fake IoA."""

import importlib.util
import asyncio
import ast
import json
import os
from pathlib import Path
from threading import Thread
import tempfile
from types import SimpleNamespace
import unittest
from enum import Enum
from unittest.mock import patch

from simulator.http_client import AgentClient, ControllerClient, SimulationHTTPError
from simulator.ioa_run import (
    round_payload, run_episode, verify_round_audit, wait_for_barrier,
)
from simulator.service import SimulationHTTPServer, SimulationService


ROOT = Path(__file__).parents[1]
SCENARIO = ROOT / "scenarios" / "pilot_success.json"
IDS = ("UAV_observer", "UUV_leader", "USV_marker")
AUTH = {
    "controller_token": "test-controller-token-000",
    "agent_tokens": {agent: f"test-{agent}-token-000" for agent in IDS},
}


class ContinuousIoARunTests(unittest.TestCase):
    def setUp(self):
        output_root = ROOT / "outputs"
        output_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=output_root)
        self.output = Path(self.temporary.name)
        self.service = SimulationService(SCENARIO, self.output, AUTH)
        self.server = SimulationHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.controller = ControllerClient(url, AUTH["controller_token"])
        self.agents = {
            agent: AgentClient(url, AUTH["agent_tokens"][agent]) for agent in IDS
        }
        self.clients = {agent: f"fake://{agent}" for agent in IDS}
        self.round_id = None
        self.session = "same-IoA-session"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def status(self, url, comm_id, timeout):
        agent = url.rsplit("/", 1)[-1]
        return {
            "agent_name": agent, "comm_id": comm_id,
            "round_id": self.round_id, "concluded": True,
            "pending_tasks": [], "failed_tasks": [], "await_agents": [],
            "message_count": 5, "max_turns": 12, "round_error": None,
        }

    def test_same_session_advances_only_after_each_round_and_captures(self):
        seen = []

        def fake_ioa(url, payload, timeout):
            state = self.controller.status()
            t = state["time"]
            self.assertEqual(payload["sim_round_id"], f'{state["episode_id"]}:{t}')
            if t == 0:
                self.assertEqual(set(payload["team_member_names"]), set(IDS))
                self.assertNotIn("comm_id", payload)
            else:
                self.assertEqual(payload["comm_id"], self.session)
                self.assertIn("cont_input", payload)
            self.round_id = payload["sim_round_id"]
            seen.append(t)
            if t % state["observation_period"] == 0:
                sample = self.agents["UAV_observer"].observe()
                if sample["target_position"] is not None:
                    for agent in IDS:
                        self.agents[agent].set_waypoint(state["episode_id"], t, sample["target_position"])
            self.agents["USV_marker"].try_tag(state["episode_id"], t)
            return [self.session, "Finished the current simulation round."]

        report = run_episode(
            self.controller, self.clients, max_rounds=0,
            launch=fake_ioa, fetch=self.status, print_progress=lambda *args: None,
        )
        self.assertIsNone(report["error"])
        self.assertEqual(report["status"], "captured")
        self.assertEqual(report["capture_time"], 18)
        self.assertEqual(seen, list(range(19)))
        self.assertEqual(report["completed_rounds"], 19)
        self.assertTrue(all(r["elapsed_wall_seconds"] >= 0 for r in report["rounds"]))
        self.assertEqual(len({r["comm_id"] for r in report["rounds"]}), 1)
        self.assertEqual(len(json.loads((self.output / report["artifacts"]["json"]).read_text())["trace"]), 19)

    def test_missing_real_tool_calls_abort_without_stepping(self):
        def fake_ioa(url, payload, timeout):
            self.round_id = payload["sim_round_id"]
            return [self.session, "I claim that I called the tools."]

        report = run_episode(
            self.controller, self.clients, launch=fake_ioa,
            fetch=self.status, print_progress=lambda *args: None,
        )
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(report["time"], 0)
        self.assertIn("required tool calls missing", report["error"])
        self.assertEqual(report["completed_rounds"], 0)

    def test_launch_timeout_reports_phase_and_keeps_clock_frozen(self):
        def timed_out(url, payload, timeout):
            raise TimeoutError("timed out")

        report = run_episode(
            self.controller, self.clients, launch=timed_out,
            fetch=self.status, print_progress=lambda *args: None,
        )
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(report["time"], 0)
        self.assertEqual(report["completed_rounds"], 0)
        self.assertEqual(report["failure_context"]["phase"], "launch_goal")
        self.assertEqual(report["failure_context"]["tool_calls_this_second"], [])
        self.assertIn("TimeoutError during launch_goal", report["error"])

    def test_three_round_smoke_stops_deliberately_after_three_steps(self):
        def fake_ioa(url, payload, timeout):
            state = self.controller.status()
            t = state["time"]
            self.round_id = payload["sim_round_id"]
            if t == 0:
                sample = self.agents["UAV_observer"].observe()
                for agent in IDS:
                    self.agents[agent].set_waypoint(state["episode_id"], t, sample["target_position"])
            self.agents["USV_marker"].try_tag(state["episode_id"], t)
            return [self.session, "Round complete"]

        report = run_episode(
            self.controller, self.clients, max_rounds=3,
            launch=fake_ioa, fetch=self.status, print_progress=lambda *args: None,
        )
        self.assertEqual(report["status"], "aborted")
        self.assertIsNone(report["error"])
        self.assertEqual(report["completed_rounds"], 3)
        self.assertEqual(report["time"], 3)

    def test_pending_task_blocks_barrier_until_completed(self):
        checks = 0

        def fetch(url, comm_id, timeout):
            nonlocal checks
            checks += 1
            self.assertEqual(self.controller.status()["time"], 0)
            result = self.status(url, comm_id, timeout)
            if checks == 1:
                result["pending_tasks"] = [{"task_id": "unfinished", "status": "IN_PROGRESS"}]
            return result

        self.controller.reset()
        self.round_id = "a" * 32 + ":0"
        import time
        statuses = wait_for_barrier(
            self.clients, self.session, self.round_id,
            time.monotonic() + 2, poll_interval=0.001, fetch=fetch,
        )
        self.assertGreater(checks, len(IDS))
        self.assertTrue(all(not x["pending_tasks"] for x in statuses.values()))
        self.assertEqual(self.controller.status()["time"], 0)

    def test_one_status_get_timeout_is_retried_before_deadline(self):
        self.controller.reset()
        self.round_id = "a" * 32 + ":0"
        checks = 0

        def fetch(url, comm_id, timeout):
            nonlocal checks
            checks += 1
            if checks == 1:
                raise TimeoutError("timed out")
            return self.status(url, comm_id, timeout)

        import time
        statuses = wait_for_barrier(
            self.clients, self.session, self.round_id,
            time.monotonic() + 2, poll_interval=0.001, fetch=fetch,
        )
        self.assertGreater(checks, len(IDS))
        self.assertTrue(all(status["concluded"] for status in statuses.values()))
        self.assertEqual(self.controller.status()["time"], 0)

    def test_failed_task_fails_barrier_before_stepping(self):
        self.controller.reset()
        self.round_id = "a" * 32 + ":0"

        def fetch(url, comm_id, timeout):
            result = self.status(url, comm_id, timeout)
            if result["agent_name"] == "USV_marker":
                result["failed_tasks"] = [{"task_id": "bad", "status": "FAILED"}]
            return result

        import time
        with self.assertRaisesRegex(RuntimeError, "failed tool tasks"):
            wait_for_barrier(
                self.clients, self.session, self.round_id,
                time.monotonic() + 2, fetch=fetch,
            )
        self.assertEqual(self.controller.status()["time"], 0)

    def test_controller_audit_is_running_only_controller_view(self):
        state = self.controller.reset()
        audit = self.controller.audit()
        self.assertEqual(audit["time"], 0)
        self.assertEqual(audit["calls"], [])
        self.assertNotIn("target_position", audit)
        with self.assertRaises(SimulationHTTPError) as exc:
            self.agents["UAV_observer"].request("GET", "/control/audit")
        self.assertEqual(exc.exception.status, 403)
        self.agents["UAV_observer"].observe()
        audit = self.controller.audit()
        self.assertEqual(audit["calls"][0]["operation"], "observe")


class RoundCounterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The helper is deliberately dependency-free; load it without importing
        # the whole IoA runtime, which is only installed in Docker on Ubuntu.
        source = ROOT.parent / "im_client" / "communication" / "sim_round_state.py"
        from simulator.tests.capture_helpers import load_helper
        cls.module = load_helper("sim_round_state")

    def test_counter_and_task_scope_reset_for_each_second(self):
        messages = ["old"] * 25
        memory = SimpleNamespace(messages=messages)
        info = SimpleNamespace(
            comm_id="one-session", memory=memory, conclusion="old conclusion",
            sim_round_error=None, sim_round_message_starts=[],
        )
        task_manager = SimpleNamespace(
            global_index=7, tasks={}, task_assign_manager=SimpleNamespace(await_agents=set()),
        )
        key = "a" * 32 + ":0"
        self.module.begin_round(info, task_manager, key, 12, "first round")
        self.assertEqual(self.module.round_message_count(info), 0)
        messages.extend(["user", "observer", "leader"])
        self.assertEqual(self.module.round_message_count(info), 3)
        task = SimpleNamespace(task_id="t", assignee="UAV_observer",
                               status=SimpleNamespace(name="IN_PROGRESS"))
        task_manager.tasks["t"] = (7, task)
        task_manager.global_index = 8
        self.assertEqual(len(self.module.round_status(info, task_manager)["pending_tasks"]), 1)
        info.conclusion = "done"
        task.status.name = "COMPLETED"
        self.module.begin_round(info, task_manager, "a" * 32 + ":1", 12, "second round")
        second = self.module.round_status(info, task_manager)
        self.assertEqual(second["message_count"], 0)
        self.assertEqual(second["task_count"], 0)
        self.assertEqual(second["sim_time"], 1)
        self.assertFalse(second["concluded"])

    def test_prompt_window_retains_latest_observation_interval(self):
        memory = SimpleNamespace(messages=[])
        info = SimpleNamespace(
            comm_id="one-session", memory=memory, conclusion=None,
            sim_round_error=None, sim_round_message_starts=[],
        )
        task_manager = SimpleNamespace(
            global_index=0, tasks={}, task_assign_manager=SimpleNamespace(await_agents=set()),
        )
        for second in range(12):
            self.module.begin_round(
                info, task_manager, "a" * 32 + f":{second}", 12, "goal",
            )
            memory.messages.append(f"round-{second}")
            if second == 10:
                self.assertEqual(memory.messages[info.sim_prompt_message_start:], [
                    f"round-{i}" for i in range(5, 11)
                ])
                self.assertEqual(self.module.round_status(info, task_manager)["prompt_message_count"], 6)
        self.assertEqual(memory.messages[info.sim_prompt_message_start:], [
            f"round-{i}" for i in range(6, 12)
        ])

    def test_malformed_discussion_reply_is_corrected_before_use(self):
        class FakeModel:
            def __init__(self):
                self.calls = []

            async def agenerate_response(self, **kwargs):
                self.calls.append(kwargs)
                content = (
                    {"thought": "checking", "message_type": "discussion",
                     "next_people": ["F5_UUV_leader"]}
                    if len(self.calls) == 1 else
                    {"thought": "checking", "message_type": "discussion",
                     "next_people": ["F5_UUV_leader"], "content": "继续协调"}
                )
                return SimpleNamespace(content=content)

        model = FakeModel()
        original_append = ["original instruction"]
        invalid = []
        reply = asyncio.run(self.module.generate_valid_discussion_response(
            model, prepend_prompt=["system"], history=[{"role": "user", "content": "go"}],
            append_prompt=original_append, allowed_types={"discussion"},
            on_invalid=lambda number, error: invalid.append((number, error)),
        ))
        self.assertEqual(reply.content["content"], "继续协调")
        self.assertEqual(invalid, [(1, "missing fields: content")])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[0]["append_prompt"], original_append)
        self.assertIn("content", model.calls[1]["append_prompt"][-1])
        self.assertEqual(original_append, ["original instruction"])

    def test_persistently_malformed_reply_fails_with_clear_reason(self):
        class FakeModel:
            def __init__(self):
                self.calls = 0

            async def agenerate_response(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(content={"thought": "x", "message_type": "discussion",
                                                "next_people": ["leader"]})

        model = FakeModel()
        with self.assertRaisesRegex(ValueError, "missing fields: content"):
            asyncio.run(self.module.generate_valid_discussion_response(
                model, prepend_prompt=[], history=[], append_prompt=[],
                allowed_types={"discussion"},
            ))
        self.assertEqual(model.calls, 2)
        self.assertEqual(
            self.module.discussion_response_error(
                {"thought": "x", "message_type": [], "next_people": [], "content": "hi"},
                {"discussion"},
            ),
            "message_type is not a supported discussion type",
        )

    def test_conclusion_without_next_speaker_is_normalized_without_retry(self):
        for omit_field in (False, True):
            with self.subTest(omit_field=omit_field):
                raw = {"thought": "done", "message_type": "conclude_group_discussion",
                       "content": "本轮任务已结束", "next_people": None}
                if omit_field:
                    del raw["next_people"]
                calls = []

                async def generate(**kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(content=raw)

                reply = asyncio.run(self.module.generate_valid_discussion_response(
                    SimpleNamespace(agenerate_response=generate),
                    prepend_prompt=[], history=[], append_prompt=[],
                    allowed_types={"conclude_group_discussion"},
                ))
                self.assertEqual(reply.content["next_people"], [])
                self.assertEqual(reply.content["message_type"], "conclude_group_discussion")
                self.assertEqual(len(calls), 1)
                self.assertIsNone(raw.get("next_people"))  # original payload not mutated
        self.assertEqual(self.module.discussion_response_error(
            {"thought": "done", "message_type": "conclude_group_discussion", "next_people": None},
            {"conclude_group_discussion"},
        ), "missing fields: content")

    def test_nonterminal_bad_next_speaker_gets_specific_feedback_and_still_fails(self):
        for speakers in (None, {"name": "leader"}, 7):
            with self.subTest(speakers=speakers):
                calls = []

                async def generate(**kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(content={
                        "thought": "continue", "message_type": "discussion",
                        "next_people": speakers, "content": "继续讨论",
                    })

                with self.assertRaisesRegex(ValueError, "next_people must be a name or list of names") as error:
                    asyncio.run(self.module.generate_valid_discussion_response(
                        SimpleNamespace(agenerate_response=generate),
                        prepend_prompt=[], history=[], append_prompt=[],
                        allowed_types={"discussion", "conclude_group_discussion"},
                    ))
                self.assertEqual(len(calls), 2)
                feedback = calls[1]["append_prompt"][-1]
                self.assertIn("next_people must be a name or list of names", feedback)
                self.assertIn("message_type=discussion", str(error.exception))
                self.assertIn(f"next_people_type={type(speakers).__name__}", str(error.exception))

    def test_capture_listener_exposes_coordination_exception_and_stays_alive(self):
        # Load only the coroutine AST because the full IoA runtime dependencies
        # are installed in Ubuntu containers, not this Windows test process.
        source = ROOT.parent / "im_client/communication/communication_layer.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        layer = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "CommunicationLayer")
        method = next(node for node in layer.body
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == "_listen_message")
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))

        class State(Enum):
            DISCUSSION = 1
            VOTE = 2

        class Websocket:
            calls = 0

            async def receive_message(self):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        comm_id="session", proposal_id="a" * 32 + ":5",
                        state=State.DISCUSSION, max_turns=24,
                    )
                raise asyncio.CancelledError()

        class Log:
            def info(self, *args): pass
            def error(self, *args): pass

        namespace = {"os": os, "logger": Log(), "CommunicationState": State}
        exec(compile(module, str(source), "exec"), namespace)
        async def malformed(*args, **kwargs):
            raise KeyError("content")
        bank = {"session": SimpleNamespace(
            sim_round_id="a" * 32 + ":5", sim_round_error=None,
        )}
        fake = SimpleNamespace(
            server_websocket=Websocket(), comm_bank=bank, name="F5_UUV_leader",
            coordination=malformed,
        )
        with patch.dict(os.environ, {"SIM_URL": "http://simulator"}):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(namespace["_listen_message"](fake))
        self.assertEqual(fake.server_websocket.calls, 2)
        self.assertIn("KeyError", bank["session"].sim_round_error)
        self.assertIn("content", bank["session"].sim_round_error)


if __name__ == "__main__":
    unittest.main()
