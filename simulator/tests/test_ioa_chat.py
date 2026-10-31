"""The finished episode should retain the IoA Server's actual group messages."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
import tempfile
import unittest

from simulator.ioa_chat import attach_chat_artifacts


COMM_ID = "5" * 32
EPISODE_ID = "6" * 32


def sample_record():
    return {
        COMM_ID: {
            "comm_id": COMM_ID,
            "agent_names": ["UAV_observer", "UUV_leader", "USV_marker"],
            "team_name": "capture team",
            "chat_record": [
                {"comm_id": COMM_ID, "proposal_id": f"{EPISODE_ID}:0",
                 "sender": "UUV_leader", "type": 5, "content": "Please observe now.",
                 "task_desc": "Call sim_observe", "task_conclusion": ""},
                {"comm_id": COMM_ID, "proposal_id": f"{EPISODE_ID}:0",
                 "sender": "UAV_observer", "type": 7, "content": "Target observed.",
                 "task_desc": "", "task_conclusion": "observed_at=0"},
                {"comm_id": COMM_ID, "proposal_id": f"{EPISODE_ID}:1",
                 "sender": "USV_marker", "type": 4, "content": "Continuing to target.",
                 "task_desc": "", "task_conclusion": ""},
            ],
        }
    }


class ChatHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received_path = self.path
        self.server.received_body = request
        encoded = json.dumps(self.server.chat_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        pass


class IoAChatExportTests(unittest.TestCase):
    def setUp(self):
        output_root = Path(__file__).parents[1] / "outputs"
        output_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=output_root)
        self.output = Path(self.temporary.name)
        self.server = HTTPServer(("127.0.0.1", 0), ChatHandler)
        self.server.chat_payload = sample_record()
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.server_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def report(self):
        return {
            "episode_id": EPISODE_ID, "comm_id": COMM_ID,
            "status": "captured", "error": None,
            "artifacts": {"html": EPISODE_ID + ".html"},
        }

    def test_exports_exact_server_record_and_readable_ordered_transcript(self):
        report = attach_chat_artifacts(self.report(), self.server_url, self.output)

        self.assertEqual(self.server.received_path, "/fetch_chat_record")
        self.assertEqual(self.server.received_body, {"comm_id": COMM_ID})
        self.assertIsNone(report["chat_export_error"])
        self.assertEqual(report["chat_message_count"], 3)
        self.assertEqual(report["status"], "captured")
        raw = self.output / report["artifacts"]["ioa_chat_json"]
        readable = self.output / report["artifacts"]["ioa_chat_txt"]
        self.assertEqual(json.loads(raw.read_text(encoding="utf-8")), sample_record())
        text = readable.read_text(encoding="utf-8")
        self.assertLess(text.index("Please observe now."), text.index("Target observed."))
        self.assertLess(text.index("Target observed."), text.index("Continuing to target."))
        self.assertIn(f"=== {EPISODE_ID}:1 ===", text)
        self.assertIn("Assigned task: Call sim_observe", text)
        self.assertIn("Task result: observed_at=0", text)

    def test_missing_server_record_is_reported_without_changing_capture_result(self):
        self.server.chat_payload = {}
        report = attach_chat_artifacts(self.report(), self.server_url, self.output)

        self.assertEqual(report["status"], "captured")
        self.assertIsNone(report["error"])
        self.assertIn("no chat record", report["chat_export_error"])
        self.assertNotIn("ioa_chat_json", report["artifacts"])

    def test_no_session_does_not_query_an_unrelated_chat(self):
        report = self.report()
        report["comm_id"] = None
        result = attach_chat_artifacts(report, self.server_url, self.output)
        self.assertIn("No IoA communication session", result["chat_export_error"])
        self.assertFalse(hasattr(self.server, "received_body"))


if __name__ == "__main__":
    unittest.main()
