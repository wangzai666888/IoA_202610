"""Exercise the real HTTP boundary on localhost; Docker and LLMs not needed."""

import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from simulator import ioa_tools
from simulator.http_client import AgentClient, ControllerClient, SimulationHTTPError
from simulator.http_run import run_http_episode
from simulator.ioa_check import run_check
from simulator.scenario import load_scenario
from simulator.scripted_run import run_scripted_episode
from simulator.service import SimulationHTTPServer, SimulationService


ROOT = Path(__file__).parents[1]
SCENARIO = ROOT / "scenarios" / "pilot_success.json"
AUTH = {
    "controller_token": "test-controller-token-000",
    "agent_tokens": {
        "UAV_observer": "test-observer-token-000",
        "UUV_leader": "test-leader-token-000",
        "USV_marker": "test-marker-token-000",
    },
}


class HTTPServiceTests(unittest.TestCase):
    def setUp(self):
        output_root = ROOT / "outputs"
        output_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=output_root)
        self.output = Path(self.temporary.name)
        if not self.output.resolve().is_relative_to(output_root.resolve()):
            raise RuntimeError("test output escaped the intended directory")
        self.service = SimulationService(SCENARIO, self.output, AUTH)
        self.server = SimulationHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.controller = ControllerClient(self.url, AUTH["controller_token"])
        self.agents = {
            agent_id: AgentClient(self.url, token)
            for agent_id, token in AUTH["agent_tokens"].items()
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def assert_http_error(self, code, call):
        with self.assertRaises(SimulationHTTPError) as error:
            call()
        self.assertEqual(error.exception.status, code)

    def test_http_episode_matches_in_process_trace_and_exports_replay(self):
        remote = run_http_episode(self.url, AUTH)
        local = run_scripted_episode(load_scenario(SCENARIO))
        self.assertEqual(remote["status"], "captured")
        self.assertEqual(remote["capture_time"], 18)
        self.assertEqual(remote["trace"], local["trace"])
        self.assertEqual(remote["events"], local["events"])
        html = self.output / remote["artifacts"]["html"]
        record = json.loads((self.output / remote["artifacts"]["json"]).read_text(encoding="utf-8"))
        self.assertTrue(html.is_file())
        self.assertEqual(record["trace"], local["trace"])
        self.assertEqual(record["status"], "captured")

    def test_identity_roles_and_ground_truth_boundaries(self):
        state = self.controller.reset()
        episode = state["episode_id"]
        leader = self.agents["UUV_leader"]
        observer = self.agents["UAV_observer"]
        self.assert_http_error(401, lambda: AgentClient(self.url, "wrong").state())
        self.assert_http_error(403, leader.observe)
        self.assert_http_error(403, lambda: leader.try_tag(episode, 0))
        self.assert_http_error(403, lambda: observer.request("GET", "/control/result"))
        self.assert_http_error(403, lambda: observer.request("POST", "/control/step", {
            "episode_id": episode, "expected_time": 0,
        }))
        self.assert_http_error(409, self.controller.result)
        own = leader.state()
        self.assertEqual(own["agent_id"], "UUV_leader")
        self.assertNotIn("target_position", own)
        self.assertNotIn("target_route", own)
        self.assertNotIn("agent_positions", own)
        self.assertEqual(observer.observe()["target_position"], [0.0, 40.0])
        self.assert_http_error(400, lambda: observer.request("POST", "/agent/waypoint", {
            "episode_id": episode, "expected_time": 0, "point": [10, 40],
            "agent_id": "UUV_leader",
        }))
        self.assertEqual(leader.state()["position"], [7.0, 45.0])

    def test_only_explicit_step_advances_and_old_actions_are_rejected(self):
        state = self.controller.reset()
        episode = state["episode_id"]
        leader = self.agents["UUV_leader"]
        leader.set_waypoint(episode, 0, [13, 45])
        for _ in range(8):
            self.assertEqual(leader.state()["time"], 0)
            self.assertEqual(self.agents["UAV_observer"].observe()["observed_at"], 0)
        self.controller.step(episode, 0)
        self.assertEqual(leader.state()["position"], [9.0, 45.0])
        self.assert_http_error(409, lambda: leader.set_waypoint(episode, 0, [1, 1]))
        self.assert_http_error(409, lambda: self.controller.step(episode, 0))
        self.controller.step(episode, 1)
        self.controller.step(episode, 2)
        self.controller.step(episode, 3)
        self.assertEqual(leader.state()["position"], [13.0, 45.0])
        self.assertIsNone(leader.state()["waypoint"])
        self.assertEqual(self.agents["UAV_observer"].observe()["observed_at"], 0)

    def test_reset_cannot_silently_erase_active_run_or_accept_previous_episode(self):
        old = self.controller.reset()
        self.assert_http_error(409, self.controller.reset)
        self.controller.abort(old["episode_id"], 0, "test interruption")
        self.assertEqual(self.controller.result()["status"], "aborted")
        new = self.controller.reset()
        self.assertNotEqual(old["episode_id"], new["episode_id"])
        self.assert_http_error(409, lambda: self.agents["USV_marker"].try_tag(old["episode_id"], 0))

    def test_http_escape_uses_same_boundary_and_exports_failure(self):
        state = self.controller.reset()
        while state["status"] == "running":
            state = self.controller.step(state["episode_id"], state["time"])
        self.assertEqual(state["status"], "escaped")
        self.assertEqual(state["time"], 116)
        result = self.controller.result()
        self.assertEqual(result["trace"][-1]["target_position"], [101.0, 55.0])
        self.assertTrue((self.output / result["artifacts"]["html"]).is_file())

    def test_ioa_tool_wrappers_use_own_token_and_preserve_observation_time(self):
        state = self.controller.reset()
        with patch.dict(os.environ, {
            "SIM_URL": self.url,
            "SIM_AGENT_TOKEN": AUTH["agent_tokens"]["UAV_observer"],
        }):
            self.assertEqual(json.loads(ioa_tools.sim_self_state())["agent_id"], "UAV_observer")
            sample = json.loads(ioa_tools.sim_observe())
            self.assertEqual(sample["observed_at"], 0)
            ioa_tools.sim_move_to(state["episode_id"], 0, 10, 40)
            self.assert_http_error(403, lambda: ioa_tools.sim_try_tag(state["episode_id"], 0))
        with patch.dict(os.environ, {
            "SIM_URL": self.url,
            "SIM_AGENT_TOKEN": AUTH["agent_tokens"]["UUV_leader"],
        }):
            self.assert_http_error(403, ioa_tools.sim_observe)
        names = lambda role: {schema["function"]["name"] for schema in ioa_tools.tool_schemas(role)}
        self.assertNotIn("sim_observe", names("leader"))
        self.assertNotIn("sim_try_tag", names("observer"))
        self.assertIn("sim_try_tag", names("marker"))

    def test_mutation_tool_uses_task_round_even_if_model_mistypes_id(self):
        state = self.controller.reset()
        episode = state["episode_id"]
        round_id = f"{episode}:0"
        wrong = episode[:-1]
        tag_schema = next(
            item["function"]["parameters"]
            for item in ioa_tools.tool_schemas("marker")
            if item["function"]["name"] == "sim_try_tag"
        )
        self.assertEqual(tag_schema["required"], [])
        with patch.dict(os.environ, {
            "SIM_URL": self.url,
            "SIM_AGENT_TOKEN": AUTH["agent_tokens"]["USV_marker"],
        }):
            arguments = ioa_tools.bind_simulation_tool_args(
                "sim_try_tag", {"episode_id": wrong, "expected_time": 99}, round_id,
            )
            self.assertEqual(arguments, {"episode_id": episode, "expected_time": 0})
            result = json.loads(ioa_tools.sim_try_tag(**arguments))
            self.assertFalse(result["accepted"])
            self.assertEqual(result["episode_id"], episode)
            self.assertIn(("USV_marker", "tag"), {
                (call["agent_id"], call["operation"])
                for call in self.controller.audit()["calls"]
            })
            self.controller.step(episode, 0)
            # The frozen old task context stays at t=0 and is rejected at t=1.
            old_arguments = ioa_tools.bind_simulation_tool_args(
                "sim_try_tag", {}, round_id,
            )
            self.assert_http_error(409, lambda: ioa_tools.sim_try_tag(**old_arguments))
            with self.assertRaises(ValueError):
                ioa_tools.bind_simulation_tool_args("sim_try_tag", {}, None)

    def test_single_tick_checker_requires_actual_calls_and_keeps_clock_frozen(self):
        def fake_ioa(url, payload, timeout):
            # Fake the IoA transport, NOT the simulator; no model is called by this test.
            self.assertNotIn("target_position", payload["goal"])
            state = self.controller.status()
            observer = self.agents["UAV_observer"]
            sample = observer.observe()
            for client in self.agents.values():
                client.set_waypoint(state["episode_id"], 0, sample["target_position"])
            self.agents["USV_marker"].try_tag(state["episode_id"], 0)
            return ["test-comm-id", "Completed the requested tools."]

        report = run_check(self.controller, "unused", launch=fake_ioa)
        self.assertTrue(report["tool_check_passed"])
        self.assertEqual(report["time"], 0)
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(len(report["tool_audit"]), 5)
        self.assertFalse(report["tool_audit"][-1]["accepted"])
        audit = json.loads((self.output / report["artifacts"]["audit"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["calls"], report["tool_audit"])
        self.assertEqual(len(self.controller.result()["trace"]), 1)

    def test_natural_language_claim_does_not_pass_single_tick_check(self):
        report = run_check(self.controller, "unused",
                           launch=lambda *args: ["fake-comm-id", "All tools completed; captured!"])
        self.assertFalse(report["tool_check_passed"])
        self.assertEqual(len(report["missing_tool_calls"]), 5)
        self.assertEqual(report["status"], "aborted")

    def test_single_tick_timeout_exports_abort_and_rejects_late_mutations(self):
        def timed_out(*args):
            raise TimeoutError("model call timed out")

        report = run_check(self.controller, "unused", launch=timed_out)
        self.assertFalse(report["tool_check_passed"])
        self.assertIn("TimeoutError", report["error"])
        self.assertEqual(report["status"], "aborted")
        self.assert_http_error(409, lambda: self.agents["USV_marker"].try_tag(report["episode_id"], 0))


if __name__ == "__main__":
    unittest.main()
