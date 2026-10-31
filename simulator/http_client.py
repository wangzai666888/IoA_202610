"""Small, dependency-free client shared by tests and future IoA tools.

Agent mutations require the episode and simulation time assigned to the task.
Do not silently refresh these values after an LLM finishes an old task.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class SimulationHTTPError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class HTTPClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        request = Request(
            self.base_url + path, data=data, method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        # No automatic mutation retries: a response timeout does not prove
        # that the server failed to apply the action.
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get("error", exc.reason)
            except (ValueError, AttributeError):
                message = exc.reason
            finally:
                exc.close()
            raise SimulationHTTPError(exc.code, str(message)) from exc


class AgentClient(HTTPClient):
    def state(self) -> dict:
        return self.request("GET", "/agent/state")

    def observe(self) -> dict:
        return self.request("GET", "/agent/observation")

    def set_waypoint(self, episode_id: str, expected_time: int, point) -> dict:
        return self.request("POST", "/agent/waypoint", {
            "episode_id": episode_id, "expected_time": expected_time,
            "point": point,
        })

    def try_tag(self, episode_id: str, expected_time: int) -> dict:
        return self.request("POST", "/agent/tag", {
            "episode_id": episode_id, "expected_time": expected_time,
        })


class ControllerClient(HTTPClient):
    def reset(self) -> dict:
        return self.request("POST", "/control/reset", {})

    def status(self) -> dict:
        return self.request("GET", "/control/status")

    def audit(self) -> dict:
        return self.request("GET", "/control/audit")

    def step(self, episode_id: str, expected_time: int) -> dict:
        return self.request("POST", "/control/step", {
            "episode_id": episode_id, "expected_time": expected_time,
        })

    def finish(self, episode_id: str, expected_time: int) -> dict:
        return self.request("POST", "/control/finish", {
            "episode_id": episode_id, "expected_time": expected_time,
        })

    def abort(self, episode_id: str, expected_time: int, reason: str) -> dict:
        return self.request("POST", "/control/abort", {
            "episode_id": episode_id, "expected_time": expected_time,
            "reason": reason,
        })

    def result(self) -> dict:
        return self.request("GET", "/control/result")
