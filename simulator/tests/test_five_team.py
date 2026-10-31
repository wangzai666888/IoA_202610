"""Nine-to-five selection and real-HTTP scheduling without model/API costs."""

import json
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest

from simulator.core import AgentState
from simulator.http_client import AgentClient, ControllerClient, SimulationHTTPError
from simulator.ioa_run import run_episode, verify_round_audit, wait_for_barrier
from simulator.prepare_team import prepare_team
from simulator.scenario import load_scenario
from simulator.scripted_run import run_scripted_episode
from simulator.service import SimulationHTTPServer, SimulationService
from simulator.team_selection import select_fixed_team


ROOT = Path(__file__).parents[1]
SCENARIO = ROOT / "scenarios/five_team.json"
IDS = ["F5_UAV_observer", "F5_USV_observer", "F5_UUV_leader", "F5_USV_marker", "F5_UAV_marker"]
AUTH = {
    "controller_token": "five-test-controller-token",
    "agent_tokens": {name: "five-test-token-" + name for name in IDS},
}


class FixedSelectionTests(unittest.TestCase):
    def test_expected_team_and_reproducible_model_free_capture(self):
        first = run_scripted_episode(load_scenario(SCENARIO))
        second = run_scripted_episode(load_scenario(SCENARIO))
        self.assertEqual(first, second)
        self.assertEqual(first["capture_time"], 18)
        selection = first["team_selection"]
        self.assertEqual(selection["selected_ids"], IDS)
        self.assertEqual(selection["candidate_count"], 9)
        self.assertEqual(selection["role_counts"], {"observer": 2, "leader": 1, "marker": 2})
        self.assertEqual(len(first["trace"][0]["agent_positions"]), 5)
        excluded = {row["id"]: row["reason"] for row in selection["candidates"] if not row["selected"]}
        self.assertEqual(excluded["F5_USV_leader"], "outside_radius")
        self.assertEqual(excluded["F5_UUV_marker"], "role_quota_filled")

    def test_distance_ties_use_id_and_do_not_depend_on_carrier_or_input_order(self):
        # All candidates have the same carrier: role coverage alone is required.
        candidates = [AgentState(name, "UAV", role, [1, 1]) for name, role in [
            ("o_c", "observer"), ("o_b", "observer"), ("o_a", "observer"),
            ("l_b", "leader"), ("l_a", "leader"),
            ("m_c", "marker"), ("m_b", "marker"), ("m_a", "marker"),
        ]]
        first, record = select_fixed_team(candidates, [0, 0], 25)
        second, other = select_fixed_team(list(reversed(candidates)), [0, 0], 25)
        self.assertEqual([a.agent_id for a in first], ["o_a", "o_b", "l_a", "m_a", "m_b"])
        self.assertEqual(record, other)
        self.assertEqual([a.agent_id for a in first], [a.agent_id for a in second])

    def test_shortage_or_duplicate_id_is_an_error(self):
        env = load_scenario(SCENARIO)
        with self.assertRaisesRegex(ValueError, "not enough marker"):
            select_fixed_team(env.agents, [0, 40], 25, radius=20)
        with self.assertRaisesRegex(ValueError, "unique"):
            select_fixed_team([*env.agents, env.agents[0]], [0, 40], 25)

    def test_all_candidate_positions_are_validated_even_if_not_selected(self):
        data = json.loads(SCENARIO.read_text(encoding="utf-8"))
        data["candidates"][-1]["position"] = [1000, 1000]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside the map"):
                load_scenario(path)


class PrepareFiveTests(unittest.TestCase):
    def test_configs_cover_selected_members_with_separate_identity_and_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = prepare_team(root, SCENARIO)
            auth = json.loads((root / "simulator/service_auth_five.local.json").read_text())
            manifest = json.loads((root / "simulator/five_clients.local.json").read_text())
            compose = json.loads((root / "dockerfiles/compose/capture_five.local.yaml").read_text())
            self.assertEqual(report["selected_ids"], IDS)
            self.assertEqual(set(auth["agent_tokens"]), set(IDS))
            self.assertEqual(len(set(manifest["clients"].values())), 5)
            self.assertEqual(len(compose["services"]), 5)
            self.assertEqual(report, prepare_team(root, SCENARIO))  # same credentials reused
            for index, member in enumerate(manifest["members"]):
                name = member["id"]
                config = json.loads((root / f"configs/client_configs/cases/capture_five.local/{name}.yaml").read_text(encoding="utf-8"))
                private = (root / f"simulator/five_team.local/{name}.env").read_text()
                self.assertEqual(config["comm"]["name"], name)
                self.assertEqual(config["tool_agent"]["agent_name"], name)
                self.assertTrue(config["tool_agent"]["tools_config"].endswith(member["role"] + ".json"))
                self.assertIn(auth["agent_tokens"][name], private)
                self.assertNotIn(auth["controller_token"], private)
                for other in set(IDS) - {name}:
                    self.assertNotIn(auth["agent_tokens"][other], private)
                self.assertEqual(manifest["clients"][name], f"http://127.0.0.1:{5070 + index}")

    def test_existing_configuration_and_old_auth_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_team(root, SCENARIO)
            config = root / "simulator/five_clients.local.json"
            config.write_text("operator-edited", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                prepare_team(root, SCENARIO)
            self.assertEqual(config.read_text(), "operator-edited")
            auth_path = root / "simulator/service_auth_five.local.json"
            original = auth_path.read_text()
            auth = json.loads(original)
            auth["agent_tokens"].pop(IDS[-1])
            auth_path.write_text(json.dumps(auth))
            with self.assertRaisesRegex(ValueError, "exactly match"):
                prepare_team(root, SCENARIO)
            self.assertEqual(json.loads(auth_path.read_text()), auth)


class FiveIoAHTTPTests(unittest.TestCase):
    def setUp(self):
        output = ROOT / "outputs"
        output.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=output)
        self.output = Path(self.temporary.name)
        self.service = SimulationService(SCENARIO, self.output, AUTH)
        self.server = SimulationHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.controller = ControllerClient(url, AUTH["controller_token"])
        self.agents = {name: AgentClient(url, token) for name, token in AUTH["agent_tokens"].items()}
        self.clients = {name: "fake://" + name for name in IDS}
        self.round_id = None
        self.session = "five-fixed-session"
        self.late_replies = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def fake_status(self, url, comm_id, timeout):
        return {"agent_name": url.split("://")[1], "comm_id": comm_id,
                "round_id": self.round_id, "concluded": True, "pending_tasks": [],
                "failed_tasks": [], "await_agents": [], "round_error": None}

    def fake_ioa(self, url, payload, timeout):
        state = self.controller.status()
        episode, t = state["episode_id"], state["time"]
        self.round_id = payload["sim_round_id"]
        self.assertEqual(url, self.clients["F5_UUV_leader"])
        self.assertEqual(payload["max_turns"], 24)
        if t == 0:
            self.assertEqual(payload["team_member_names"], IDS)
        else:
            self.assertEqual(payload["comm_id"], self.session)
        self.assertNotIn("target_position", payload["goal"])
        if t % state["observation_period"] == 0:
            samples = [self.agents[name].observe() for name in IDS[:2]]
            visible = [sample for sample in samples if sample["target_position"] is not None]
            if visible:
                for client in self.agents.values():
                    client.set_waypoint(episode, t, visible[0]["target_position"])
        for marker in IDS[-2:]:
            reply = self.agents[marker].try_tag(episode, t)
            if reply.get("reason") == "already_captured":
                self.late_replies.append(reply)
        self.assertEqual(self.controller.status()["time"], t)
        return [self.session, "All five completed this round"]

    def run_fake(self, **kwargs):
        return run_episode(self.controller, self.clients, launch=self.fake_ioa,
                           fetch=self.fake_status, print_progress=lambda *args: None, **kwargs)

    def test_five_full_run_captures_and_exports_selection_and_single_capture(self):
        report = self.run_fake(max_rounds=0)
        self.assertIsNone(report["error"])
        self.assertEqual(report["status"], "captured")
        self.assertEqual(report["capture_time"], 18)
        self.assertEqual(report["completed_rounds"], 19)
        self.assertTrue(all(len(row["participants"]) == 5 for row in report["rounds"]))
        self.assertEqual(len(self.late_replies), 1)
        self.assertFalse(self.late_replies[0]["accepted"])
        result = self.controller.result()
        self.assertEqual(result["trace"], run_scripted_episode(load_scenario(SCENARIO))["trace"])
        self.assertEqual(sum(call.get("accepted", False) for call in result["tool_audit"]), 1)
        selection = json.loads((self.output / report["artifacts"]["selection"]).read_text(encoding="utf-8"))
        self.assertEqual(selection["selected_ids"], IDS)
        audit_before = self.controller.audit()
        episode = report["episode_id"]
        self.agents["F5_UAV_marker"].try_tag(episode, 18)
        self.assertEqual(self.controller.audit(), audit_before)
        for caller, t in [("F5_UAV_observer", 18), ("F5_UAV_marker", 17)]:
            with self.assertRaises(SimulationHTTPError):
                self.agents[caller].try_tag(episode, t)
        with self.assertRaises(SimulationHTTPError):
            self.agents["F5_UAV_marker"].set_waypoint(episode, 18, [1, 40])

    def test_three_round_smoke_keeps_all_five_and_stops_as_planned(self):
        report = self.run_fake(max_rounds=3)
        self.assertEqual((report["status"], report["time"], report["completed_rounds"]), ("aborted", 3, 3))
        self.assertIsNone(report["error"])
        self.assertEqual(len(report["members"]), 5)

    def test_omitting_fifth_client_rejects_before_any_model_call(self):
        self.clients.pop("F5_UAV_marker")
        report = self.run_fake()
        self.assertIn("exactly match", report["error"])
        self.assertEqual(report["completed_rounds"], 0)
        self.assertEqual(report["time"], 0)

    def test_second_observer_and_second_marker_are_required_in_a_running_round(self):
        state = self.controller.reset()
        self.fake_ioa(self.clients["F5_UUV_leader"], {
            "sim_round_id": "test:0", "team_member_names": IDS, "max_turns": 24, "goal": "",
        }, 10)
        audit = self.controller.audit()
        for member, operation in [("F5_USV_observer", "observe"), ("F5_UAV_marker", "tag")]:
            incomplete = {**audit, "calls": [call for call in audit["calls"]
                if (call["agent_id"], call["operation"]) != (member, operation)]}
            errors = verify_round_audit(incomplete, state["episode_id"], 0, 5, state["members"])
            self.assertIn(member + ":" + operation, errors)
        self.assertEqual(self.controller.status()["time"], 0)

    def test_initial_early_tag_is_allowed_only_when_each_marker_tags_after_all_waypoints(self):
        state = self.controller.reset()
        episode = state["episode_id"]
        for observer in ("F5_UAV_observer", "F5_USV_observer"):
            self.agents[observer].observe()
        for member in ("F5_UAV_observer", "F5_UAV_marker", "F5_USV_marker"):
            self.agents[member].set_waypoint(episode, 0, [10, 40])
        self.agents["F5_USV_marker"].try_tag(episode, 0)  # premature request
        for member in ("F5_USV_observer", "F5_UUV_leader"):
            self.agents[member].set_waypoint(episode, 0, [10, 40])
        self.agents["F5_UAV_marker"].try_tag(episode, 0)
        early_only = self.controller.audit()
        self.assertEqual(
            verify_round_audit(early_only, episode, 0, 5, state["members"]),
            ["F5_USV_marker has no tag after all initial waypoints"],
        )
        self.agents["F5_USV_marker"].try_tag(episode, 0)  # valid later request
        complete = self.controller.audit()
        self.assertEqual(verify_round_audit(complete, episode, 0, 5, state["members"]), [])
        self.assertEqual(self.controller.status()["time"], 0)

    def test_fifth_members_pending_task_prevents_advance(self):
        self.controller.reset()
        self.round_id = "test:0"
        checks = 0
        def pending_then_done(url, comm_id, timeout):
            nonlocal checks
            checks += 1
            self.assertEqual(self.controller.status()["time"], 0)
            status = self.fake_status(url, comm_id, timeout)
            if checks == 5:
                status["pending_tasks"] = [{"task_id": "fifth-still-working"}]
            return status
        wait_for_barrier(self.clients, self.session, self.round_id, time.monotonic() + 3,
                         poll_interval=0.001, fetch=pending_then_done)
        self.assertEqual(checks, 10)
        self.assertEqual(self.controller.status()["time"], 0)


if __name__ == "__main__":
    unittest.main()
