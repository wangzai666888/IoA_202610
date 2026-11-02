"""Frozen layout reproducibility, deployment identity and actual HTTP preflight."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import random
import shutil
import tempfile
from threading import Thread
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from common.communication_windows import pair_catalog
from simulator.core import AgentState
from simulator.fixed_scenarios import ROOT, SCENE_DIR, generate_suite, fixed_scene, scene_members
from simulator.http_client import AgentClient, ControllerClient, SimulationHTTPError
from simulator.ioa_window_run import main, run_window_episode, experiment_snapshot
from simulator.prepare_fixed_scenarios import prepare_scene, CONFIG_DIR, TEMPLATE_DIR, RUNTIME_DIR
from simulator.scenario import load_scenario
from simulator.service import SimulationHTTPServer, SimulationService
from simulator.team_selection import select_fixed_team


class FrozenSceneTests(unittest.TestCase):
    def test_saved_scenes_reproduce_every_draw_and_only_require_initial_discovery(self):
        seen_members, far_members = set(), []
        for number in range(1, 11):
            path, env, record = fixed_scene(number)
            data = json.loads(path.read_text())
            self.assertEqual(data["selection"]["radius"], None)
            self.assertEqual(data["config"]["width"], 100)
            self.assertEqual(data["config"]["height"], 100)
            self.assertEqual(len(data["candidates"]), 9)
            rng = random.Random(record["seed"])
            for attempt in range(1, record["attempts"] + 1):
                drawn = [[rng.uniform(0, 100), rng.uniform(0, 100)] for _ in range(9)]
                visible = any(c["role"] == "observer" and
                              ((p[0] - 0)**2 + (p[1] - 40)**2)**0.5 <= 25
                              for c, p in zip(data["candidates"], drawn))
                self.assertEqual(visible, attempt == record["attempts"])
            self.assertEqual(drawn, [c["position"] for c in data["candidates"]])
            self.assertEqual(record["pairs"], pair_catalog(scene_members(env)))
            seen_members.update(m["id"] for m in record["members"])
            far_members.extend(c for c in env.team_selection["candidates"] if c["selected"] and c["distance"] > 50)
            self.assertEqual(len(record["pairs"]), 10)
        self.assertEqual(len(seen_members), 9)
        self.assertTrue(far_members)  # Whole-map selection genuinely matters in this suite.

    def test_generation_is_frozen_idempotent_and_rejects_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            generate_suite(ROOT / "simulator/scenarios/five_team.json", output)
            for original in (ROOT / SCENE_DIR).glob("*.json"):
                self.assertEqual(original.read_bytes(), (output / original.name).read_bytes())
            generate_suite(ROOT / "simulator/scenarios/five_team.json", output)
            first = (output / "scene_01.json").read_bytes()
            with self.assertRaises(FileExistsError):
                generate_suite(ROOT / "simulator/scenarios/five_team.json", output, base_seed=1)
            self.assertEqual((output / "scene_01.json").read_bytes(), first)

    def test_whole_map_keeps_role_quota_and_initial_visibility_rules(self):
        candidates = [AgentState(n, "UAV", role, point) for n, role, point in [
            ("o1", "observer", [1, 40]), ("o2", "observer", [90, 90]), ("o3", "observer", [99, 99]),
            ("l1", "leader", [80, 40]), ("l2", "leader", [90, 40]), ("l3", "leader", [99, 40]),
            ("m1", "marker", [70, 40]), ("m2", "marker", [80, 40]), ("m3", "marker", [90, 40]),
        ]]
        selected, record = select_fixed_team(candidates, [0, 40], 25)
        self.assertEqual([a.agent_id for a in selected], ["o1", "o2", "l1", "m1", "m2"])
        self.assertIsNone(record["radius"])
        with self.assertRaisesRegex(ValueError, "not enough"):
            select_fixed_team(candidates, [0, 40], 25, radius=50)
        candidates[0].pos = (80, 80)
        with self.assertRaisesRegex(ValueError, "discover"):
            select_fixed_team(candidates, [0, 40], 25)

    def test_pair_listing_needs_no_runtime_bundle_or_network(self):
        with patch("simulator.ioa_window_run.urlopen", side_effect=AssertionError("network used")):
            for number in range(1, 11):
                out = io.StringIO()
                with redirect_stdout(out):
                    main(["--scene", str(number), "--list-pairs"])
                self.assertIn(f"random_fixed_{number:02d}", out.getvalue())
                self.assertEqual(len(out.getvalue().splitlines()), 11)


class FixedBundleTests(unittest.TestCase):
    def test_bundles_preserve_models_bind_only_selected_team_and_keep_tokens_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / SCENE_DIR, root / SCENE_DIR)
            shutil.copytree(ROOT / TEMPLATE_DIR, root / TEMPLATE_DIR)
            reference = json.loads(next((root / TEMPLATE_DIR).glob("*observer.yaml")).read_text())
            manifests, tool_slots = [], set()
            for number in range(1, 11):
                manifest = prepare_scene(number, root=root)
                manifests.append(manifest)
                self.assertEqual(manifest, prepare_scene(number, root=root))
                bundle = root / RUNTIME_DIR / f"scene_{number:02d}"
                auth = json.loads((bundle / "auth.local.json").read_text())
                self.assertEqual(set(auth["agent_tokens"]), set(manifest["clients"]))
                self.assertEqual((bundle / "auth.local.json").stat().st_mode & 0o777, 0o600)
                compose = json.loads((bundle / "compose.yaml").read_text())
                self.assertEqual(len(compose["services"]), 6)
                replay_mounts = [mount for mount in compose["services"]["simulator"]["volumes"]
                                 if mount["target"] == "/app/simulator/replay_template.html"]
                self.assertEqual(len(replay_mounts), 1)
                self.assertEqual(replay_mounts[0]["source"],
                                 str(root / "simulator/replay_template.html"))
                self.assertTrue(replay_mounts[0]["read_only"])
                for index, member in enumerate(manifest["members"]):
                    name = member["id"]
                    config = json.loads((root / manifest["client_configs"][name]).read_text())
                    self.assertEqual(config["comm"]["name"], name)
                    self.assertEqual(config["tool_agent"]["agent_name"], name)
                    self.assertEqual(config["tool_agent"]["model"], reference["tool_agent"]["model"])
                    self.assertEqual(config["comm"]["llm"], reference["comm"]["llm"])
                    self.assertEqual(Path(config["tool_agent"]["tools_config"]).stem, member["role"])
                    tool_slots.add(config["tool_agent"]["container_name"])
                    private = (bundle / (name + ".env")).read_text()
                    self.assertIn(auth["agent_tokens"][name], private)
                    self.assertNotIn(auth["controller_token"], private)
                    for other in set(auth["agent_tokens"]) - {name}:
                        self.assertNotIn(auth["agent_tokens"][other], private)
                    mounts = compose["services"][f"slot-{index}"]["volumes"]
                    self.assertFalse(any("auth.local.json" in m["source"] or "scene.json" in m["source"] for m in mounts))
                configs = [root / p for p in manifest["client_configs"].values()]
                snapshot = experiment_snapshot(root, scenario_path=root / manifest["scenario_file"], client_configs=configs)
                self.assertIn(manifest["scenario_file"], snapshot["source_sha256"])
                self.assertEqual(set(snapshot["models"]), set(manifest["clients"]))
                self.assertFalse(any("auth" in p or p.endswith(".env") for p in snapshot["source_sha256"]))
            self.assertEqual(len(tool_slots), 5)
            # A change to operator-managed model configuration cannot silently
            # overwrite a bundle frozen for an earlier condition.
            config_path = root / next(iter(manifests[0]["client_configs"].values()))
            config_path.write_text("operator-edited")
            with self.assertRaises(FileExistsError): prepare_scene(1, root=root)
            self.assertEqual(config_path.read_text(), "operator-edited")


class ScenePreflightTests(unittest.TestCase):
    def test_same_team_wrong_scene_is_rejected_before_reset_and_metadata_is_exported(self):
        first_path, _, first = fixed_scene(1)
        _, _, second = fixed_scene(2)
        self.assertEqual(first["members"], second["members"])  # IDs alone cannot detect this mistake.
        auth = {"controller_token": "fixed-controller-test",
                "agent_tokens": {m["id"]: "fixed-test-" + m["id"] for m in first["members"]}}
        with tempfile.TemporaryDirectory() as directory:
            service = SimulationService(first_path, Path(directory), auth)
            server = SimulationHTTPServer(("127.0.0.1", 0), service)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                controller = ControllerClient(url, auth["controller_token"])
                self.assertEqual(controller.scenario()["scenario_sha256"], first["scenario_sha256"])
                self.assertEqual(controller.status()["status"], "idle")
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    run_window_episode(controller, {m["id"]: "fake://" + m["id"] for m in first["members"]},
                        "fake://server", expected_members=first["members"], expected_scenario=second,
                        request=lambda *a, **k: self.fail("IoA request before scenario validation"))
                self.assertEqual(controller.status()["status"], "idle")
                agent = AgentClient(url, next(iter(auth["agent_tokens"].values())))
                with self.assertRaises(SimulationHTTPError) as caught:
                    agent.request("GET", "/control/scenario")
                self.assertEqual(caught.exception.status, 403)
                state = controller.reset()
                self.assertEqual(state["scenario_sha256"], first["scenario_sha256"])
                controller.abort(state["episode_id"], 0, "metadata test")
                result = controller.result()
                self.assertEqual(result["generation"]["seed"], first["seed"])
                trace = json.loads((Path(directory) / result["artifacts"]["json"]).read_text())
                self.assertEqual(trace["scenario_sha256"], first["scenario_sha256"])
                self.assertEqual(trace["generation"]["attempts"], first["attempts"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
