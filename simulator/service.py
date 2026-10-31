"""Single-process research HTTP service; only the controller advances time.

Run with python -m simulator.service. Uses the Python standard library so
the same code runs on Windows and in the Ubuntu Docker deployment.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
from threading import RLock
from uuid import uuid4

from simulator.replay import write_replay
from simulator.scenario import load_scenario


ROOT = Path(__file__).parent
DEFAULT_AUTH = ROOT / "service_auth.local.json"
DEFAULT_SCENARIO = ROOT / "scenarios" / "pilot_success.json"
LOG = logging.getLogger("simulator.service")


def reject_json_constant(value: str):
    raise ValueError("non-finite JSON number: " + value)


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def validate_auth(auth: dict, agent_ids: set[str]) -> None:
    if not isinstance(auth, dict) or set(auth) != {"controller_token", "agent_tokens"}:
        raise ValueError("auth requires controller_token and agent_tokens")
    tokens = auth["agent_tokens"]
    if not isinstance(tokens, dict) or set(tokens) != agent_ids:
        raise ValueError("auth agent IDs must exactly match the scenario members")
    all_tokens = [auth["controller_token"], *tokens.values()]
    if any(
        not isinstance(token, str) or len(token) < 16 or not token.isascii()
        or any(c.isspace() for c in token)
        for token in all_tokens
    ):
        raise ValueError("tokens must be ASCII strings of at least 16 characters without spaces")
    if len(set(all_tokens)) != len(all_tokens):
        raise ValueError("controller and agents must have different tokens")


def create_auth_file(path: Path, scenario: Path) -> None:
    env = load_scenario(scenario)
    auth = {
        "controller_token": secrets.token_urlsafe(32),
        "agent_tokens": {
            agent.agent_id: secrets.token_urlsafe(32) for agent in env.agents
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not replace credentials used by running clients.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(auth, stream, indent=2)
        stream.write("\n")


class SimulationService:
    def __init__(self, scenario: Path, output_dir: Path, auth: dict):
        self.scenario = Path(scenario)
        self.output_dir = Path(output_dir)
        self.auth = auth
        self.env = None
        self.episode_id = None
        self.artifacts = None
        self.tool_audit = []
        self.lock = RLock()
        validate_auth(auth, {a.agent_id for a in load_scenario(self.scenario).agents})

    def _identity(self, header: str) -> tuple[str, str | None]:
        if not header.startswith("Bearer "):
            raise APIError(401, "a Bearer token is required")
        token = header[7:]
        if not token.isascii():
            raise APIError(401, "invalid token")
        if hmac.compare_digest(token, self.auth["controller_token"]):
            return "controller", None
        for agent_id, candidate in self.auth["agent_tokens"].items():
            if hmac.compare_digest(token, candidate):
                return "agent", agent_id
        raise APIError(401, "invalid token")

    def _status(self) -> dict:
        if self.env is None:
            return {"status": "idle", "episode_id": None}
        return {
            "episode_id": self.episode_id,
            "scenario_id": self.env.scenario_id,
            "time": self.env.time,
            "status": self.env.status,
            "capture_time": self.env.capture_time,
            "observation_period": self.env.config.observation_period,
            "max_time": self.env.config.max_time,
            "members": [
                {"id": a.agent_id, "role": a.role, "carrier_type": a.carrier_type}
                for a in self.env.agents
            ],
        }

    @staticmethod
    def _fields(body: dict, fields: set[str]) -> None:
        if not isinstance(body, dict) or set(body) != fields:
            raise APIError(400, "expected exactly these fields: " + ", ".join(sorted(fields)))

    def _current_task(self, body: dict, *, allow_captured_tag: bool = False) -> None:
        if body["episode_id"] != self.episode_id:
            raise APIError(409, "stale episode_id")
        if type(body["expected_time"]) is not int:
            raise APIError(400, "expected_time must be an integer simulation second")
        if body["expected_time"] != self.env.time:
            raise APIError(409, "stale expected_time; discard or review the old task")
        if self.env.status != "running" and not (allow_captured_tag and self.env.status == "captured"):
            raise APIError(409, "episode has already ended")

    def _export_if_done(self) -> None:
        if self.env.status != "running" and self.artifacts is None:
            try:
                html, record = write_replay(
                    self.env, self.output_dir / f"{self.episode_id}.html"
                )
                audit_file = self.output_dir / f"{self.episode_id}.audit.json"
                audit_file.write_text(json.dumps({
                    "episode_id": self.episode_id, "calls": self.tool_audit,
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                selection_file = None
                if self.env.team_selection is not None:
                    selection_file = self.output_dir / f"{self.episode_id}.selection.json"
                    selection_file.write_text(
                        json.dumps(self.env.team_selection, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except OSError as exc:
                LOG.exception("episode ended but replay export failed")
                raise APIError(500, "episode ended but replay export failed; inspect service logs") from exc
            self.artifacts = {"html": html.name, "json": record.name, "audit": audit_file.name}
            if selection_file is not None:
                self.artifacts["selection"] = selection_file.name

    def dispatch(self, method: str, path: str, authorization: str, body: dict) -> dict:
        # Serialize mutations and snapshots; no background time thread.
        with self.lock:
            if method == "GET" and path == "/health":
                return {"status": "ok", "api_version": 1}
            role, agent_id = self._identity(authorization)
            if path.startswith("/control/"):
                if role != "controller":
                    raise APIError(403, "controller endpoint")
            elif path.startswith("/agent/"):
                if role != "agent":
                    raise APIError(403, "use the individual agent token")
            else:
                raise APIError(404, "unknown endpoint")

            if method == "GET" and path == "/control/status":
                return self._status()
            if method == "POST" and path == "/control/reset":
                self._fields(body, set())
                if self.env is not None and self.env.status == "running":
                    raise APIError(409, "finish or explicitly abort the active episode first")
                if self.env is not None:
                    self._export_if_done()
                env = load_scenario(self.scenario)
                validate_auth(self.auth, {a.agent_id for a in env.agents})
                self.env = env
                self.episode_id = uuid4().hex
                self.artifacts = None
                self.tool_audit = []
                return self._status()

            if self.env is None:
                raise APIError(409, "controller must reset before starting an episode")
            if method == "GET":
                if path == "/agent/state":
                    return {
                        **self.env.self_state(agent_id),
                        "episode_id": self.episode_id, "status": self.env.status,
                    }
                if path == "/agent/observation":
                    response = {
                        **self.env.observe(agent_id),
                        "episode_id": self.episode_id, "time": self.env.time,
                    }
                    if self.env.status == "running":
                        self.tool_audit.append({
                            "agent_id": agent_id, "time": self.env.time,
                            "operation": "observe", "observed_at": response["observed_at"],
                        })
                    return response
                if path == "/control/audit":
                    # Trusted controller can verify tool actions before stepping.
                    # It contains no hidden target route or target position.
                    return {
                        "episode_id": self.episode_id,
                        "time": self.env.time,
                        "status": self.env.status,
                        "calls": list(self.tool_audit),
                    }
                if path == "/control/result":
                    if self.env.status == "running":
                        raise APIError(409, "ground-truth result is available after the episode")
                    self._export_if_done()
                    return {
                        **self.env.result(), "episode_id": self.episode_id,
                        "artifacts": self.artifacts,
                        "tool_audit": list(self.tool_audit),
                    }
                raise APIError(404, "unknown endpoint")

            fields = {
                "/agent/waypoint": {"episode_id", "expected_time", "point"},
                "/agent/tag": {"episode_id", "expected_time"},
                "/control/step": {"episode_id", "expected_time"},
                "/control/finish": {"episode_id", "expected_time"},
                "/control/abort": {"episode_id", "expected_time", "reason"},
            }
            if method != "POST" or path not in fields:
                raise APIError(404, "unknown endpoint")
            self._fields(body, fields[path])
            self._current_task(body, allow_captured_tag=(path == "/agent/tag"))
            if path == "/agent/tag" and self.env.status == "captured":
                if self.env.self_state(agent_id)["role"] != "marker":
                    raise APIError(403, "only marker agents can request a tag")
                # A concurrent second marker can acknowledge a terminal result.
                # This is a read-only reply: no second capture or audit mutation.
                return {"accepted": False, "reason": "already_captured",
                        "time": self.env.time, "episode_id": self.episode_id,
                        "status": "captured"}
            response = None
            if path == "/agent/waypoint":
                response = self.env.set_waypoint(agent_id, body["point"])
                self.tool_audit.append({
                    "agent_id": agent_id, "time": self.env.time,
                    "operation": "waypoint", "point": body["point"],
                })
            elif path == "/agent/tag":
                response = self.env.try_tag(agent_id)
                self.tool_audit.append({
                    "agent_id": agent_id, "time": self.env.time,
                    "operation": "tag", "accepted": response["accepted"],
                })
            elif path == "/control/step":
                self.env.advance_one_second()
            elif path == "/control/finish":
                if self.env.time < self.env.config.max_time:
                    raise APIError(409, "finish is only for the simulation time limit")
                self.env.finish_current_time()
            elif path == "/control/abort":
                if not isinstance(body["reason"], str) or not body["reason"].strip():
                    raise APIError(400, "abort requires a nonempty reason")
                self.env.status = "aborted"
                self.env.events.append({
                    "time": self.env.time, "type": "aborted", "reason": body["reason"],
                })
            self._export_if_done()
            if response is None:
                return self._status()
            return {
                **response, "episode_id": self.episode_id, "status": self.env.status,
            }


class SimulationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: SimulationService):
        self.service = service
        super().__init__(address, SimulationHandler)


class SimulationHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Do not log headers, tokens, or user-supplied task bodies.
        return

    def _handle(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                raise APIError(400, "request body is too large")
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "chunked requests are not supported")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw, parse_constant=reject_json_constant)
            result = self.server.service.dispatch(
                self.command, self.path, self.headers.get("Authorization", ""), body
            )
            status = 200
        except APIError as exc:
            status, result = exc.status, {"error": str(exc)}
        except PermissionError as exc:
            status, result = 403, {"error": str(exc)}
        except (ValueError, TypeError, UnicodeError) as exc:
            status, result = 400, {"error": str(exc)}
        except RuntimeError as exc:
            status, result = 409, {"error": str(exc)}
        except Exception:
            LOG.exception("simulation service request failed")
            status, result = 500, {"error": "internal service error; inspect service logs"}
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--init-auth", action="store_true", help="create tokens without starting the service")
    args = parser.parse_args()
    if args.init_auth:
        create_auth_file(args.auth_file, args.scenario)
        print("Created auth file. Keep it private; give each agent only its own token.")
        return
    auth = json.loads(args.auth_file.read_text(encoding="utf-8"))
    service = SimulationService(args.scenario, args.output_dir, auth)
    server = SimulationHTTPServer((args.host, args.port), service)
    print(f"Simulation HTTP service listening on {args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
