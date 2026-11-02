"""Behavioral checks for the model-free pilot, using only stdlib unittest."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from simulator.core import AgentState, CaptureEnvironment, SimConfig
from simulator.replay import render_replay_html, write_replay
from simulator.scenario import load_scenario
from simulator.scripted_run import run_scripted_episode


SCENARIO = Path(__file__).parents[1] / "scenarios" / "pilot_success.json"
ROUTE = [(0, 40), (12, 40), (12, 55), (101, 55)]


def close_team(*, max_time: int = 120) -> CaptureEnvironment:
    return CaptureEnvironment(
        config=SimConfig(max_time=max_time),
        agents=[
            AgentState("obs", "UAV", "observer", (0, 40)),
            AgentState("lead", "USV", "leader", (0, 40)),
            AgentState("mark", "UUV", "marker", (0, 40)),
        ],
        target_route=copy.deepcopy(ROUTE),
    )


class CaptureCoreTests(unittest.TestCase):
    def test_scripted_pilot_captures_after_first_turn_reproducibly(self) -> None:
        first = run_scripted_episode(load_scenario(SCENARIO))
        second = run_scripted_episode(load_scenario(SCENARIO))
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "captured")
        self.assertGreater(first["capture_time"], 12)
        self.assertLess(first["capture_time"], 120)
        self.assertEqual(first["capture_time"], first["time"])
        self.assertEqual(first["trace"][0]["target_position"], [0.0, 40.0])

    def test_target_moves_through_turn_and_exits_boundary(self) -> None:
        env = load_scenario(SCENARIO)
        for _ in range(12):
            env.advance_one_second()
        self.assertEqual(env.trace[-1]["target_position"], [12.0, 40.0])
        for _ in range(15):
            env.advance_one_second()
        self.assertEqual(env.trace[-1]["target_position"], [12.0, 55.0])
        while env.status == "running":
            env.advance_one_second()
        self.assertEqual(env.status, "escaped")
        self.assertEqual(env.time, 116)
        self.assertEqual(env.trace[-2]["target_position"], [100.0, 55.0])
        self.assertEqual(env.trace[-1]["target_position"], [101.0, 55.0])

    def test_only_observer_can_read_cached_local_sample(self) -> None:
        env = load_scenario(SCENARIO)
        first = env.observe("UAV_observer")
        self.assertEqual(first["observed_at"], 0)
        self.assertEqual(first["target_position"], [0.0, 40.0])
        with self.assertRaises(PermissionError):
            env.observe("UUV_leader")
        env.advance_one_second()
        self.assertEqual(env.observe("UAV_observer"), first)
        for _ in range(4):
            env.advance_one_second()
        newer = env.observe("UAV_observer")
        self.assertEqual(newer["observed_at"], 5)
        self.assertEqual(newer["target_position"], [5.0, 40.0])
        newer["target_position"][0] = 999
        self.assertEqual(env.observe("UAV_observer")["target_position"], [5.0, 40.0])

        far = CaptureEnvironment(
            SimConfig(),
            [
                AgentState("near", "UAV", "observer", (5, 40)),
                AgentState("far", "UUV", "observer", (90, 90)),
                AgentState("lead", "USV", "leader", (7, 45)),
                AgentState("mark", "USV", "marker", (38, 70)),
            ],
            ROUTE,
        )
        self.assertIsNone(far.observe("far")["target_position"])

    def test_waypoint_persists_and_arrival_holds_position(self) -> None:
        env = load_scenario(SCENARIO)
        env.set_waypoint("UUV_leader", (13, 45))
        env.advance_one_second()
        self.assertEqual(env.self_state("UUV_leader")["position"], [9.0, 45.0])
        self.assertEqual(env.self_state("UUV_leader")["waypoint"], [13.0, 45.0])
        env.advance_one_second()
        env.advance_one_second()
        self.assertEqual(env.self_state("UUV_leader")["position"], [13.0, 45.0])
        self.assertIsNone(env.self_state("UUV_leader")["waypoint"])
        env.advance_one_second()
        self.assertEqual(env.self_state("UUV_leader")["position"], [13.0, 45.0])
        with self.assertRaises(ValueError):
            env.set_waypoint("UUV_leader", (101, 45))

    def test_tag_needs_explicit_request_and_three_consecutive_seconds(self) -> None:
        env = close_team()
        self.assertFalse(env.try_tag("mark")["accepted"])
        with self.assertRaises(PermissionError):
            env.try_tag("lead")
        for _ in range(2):
            env.advance_one_second()
            self.assertFalse(env.try_tag("mark")["accepted"])
        env.advance_one_second()
        self.assertEqual(env.status, "running")  # no automatic capture
        self.assertTrue(env.try_tag("mark")["accepted"])
        self.assertEqual(env.capture_time, 3)

    def test_support_interruption_resets_hold(self) -> None:
        env = close_team()
        env.advance_one_second()
        self.assertEqual(env.trace[-1]["support_streaks"]["mark"], 1)
        env.set_waypoint("mark", (0, 100))
        for _ in range(3):
            env.advance_one_second()
        self.assertEqual(env.trace[-1]["support_streaks"]["mark"], 0)
        self.assertFalse(env.try_tag("mark")["accepted"])

    def test_final_tick_tag_counts_before_timeout(self) -> None:
        env = close_team(max_time=3)
        for _ in range(3):
            env.advance_one_second()
        self.assertEqual(env.status, "running")
        self.assertTrue(env.try_tag("mark")["accepted"])
        env.finish_current_time()
        self.assertEqual(env.status, "captured")

        no_request = close_team(max_time=3)
        for _ in range(3):
            no_request.advance_one_second()
        no_request.finish_current_time()
        self.assertEqual(no_request.status, "timeout")

    def test_rejects_interior_appearance_and_non_turning_route(self) -> None:
        agents = close_team().agents
        with self.assertRaisesRegex(ValueError, "boundary"):
            CaptureEnvironment(SimConfig(), copy.deepcopy(agents),
                               [(50, 50), (60, 50), (101, 50)])
        with self.assertRaisesRegex(ValueError, "change direction"):
            CaptureEnvironment(SimConfig(), copy.deepcopy(agents),
                               [(0, 40), (50, 40), (101, 40)])

    def test_completed_episode_exports_standalone_replay_and_json(self) -> None:
        env = load_scenario(SCENARIO)
        run_scripted_episode(env)
        output_root = SCENARIO.parents[1] / "outputs"
        output_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(
                output_root.resolve()
            ))
            html_path, json_path = write_replay(
                env, Path(directory) / "pilot.html"
            )
            record = json.loads(json_path.read_text(encoding="utf-8"))
            html = html_path.read_text(encoding="utf-8")
            self.assertEqual(record["status"], "captured")
            self.assertEqual(record["end_time"], 18)
            self.assertEqual(len(record["trace"]), 19)
            self.assertEqual(len(record["agents"]), 3)
            self.assertIn("const replay = {", html)
            self.assertIn("pilot_success_boundary_route", html)
            self.assertIn('id="show-observation"', html)
            self.assertIn("replay.config.observation_radius * scale", html)
            self.assertEqual(render_replay_html(record), html)
            self.assertNotIn("/*__REPLAY_DATA__*/", html)
            self.assertNotIn("http://", html)
            self.assertNotIn("https://", html)
        unfinished = load_scenario(SCENARIO)
        with self.assertRaises(ValueError):
            write_replay(unfinished, Path(directory) / "unfinished.html")


if __name__ == "__main__":
    unittest.main()
