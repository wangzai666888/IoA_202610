import json
from pathlib import Path
import tempfile
import unittest

from simulator.prepare_ioa import prepare, ROLE_IDS


class CredentialPreparationTests(unittest.TestCase):
    def setUp(self):
        self.auth = {
            "controller_token": "test-controller-private-000",
            "agent_tokens": {agent: f"test-{role}-private-000" for role, agent in ROLE_IDS.items()},
        }

    def test_each_role_gets_only_its_own_identity_and_repeated_run_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare(self.auth, Path(directory))
            self.assertEqual(paths, prepare(self.auth, Path(directory)))
            for role, agent_id in ROLE_IDS.items():
                text = (Path(directory) / f"{role}.local.env").read_text(encoding="utf-8")
                self.assertIn(self.auth["agent_tokens"][agent_id], text)
                self.assertNotIn(self.auth["controller_token"], text)
                for other, token in self.auth["agent_tokens"].items():
                    if other != agent_id:
                        self.assertNotIn(token, text)

    def test_mismatched_file_is_preserved_and_other_files_are_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "leader.local.env"
            path.write_text("existing private settings", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                prepare(self.auth, Path(directory))
            self.assertEqual(path.read_text(encoding="utf-8"), "existing private settings")
            self.assertFalse((Path(directory) / "observer.local.env").exists())

    def test_dotenv_expansion_and_wrong_scenario_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = json.loads(json.dumps(self.auth))
            auth["agent_tokens"]["UAV_observer"] = "test-dollar-token-$HOME"
            with self.assertRaises(ValueError):
                prepare(auth, Path(directory))
            auth["agent_tokens"].pop("UUV_leader")
            with self.assertRaises(ValueError):
                prepare(auth, Path(directory))


if __name__ == "__main__":
    unittest.main()
