"""Deterministic, model-free 2-D capture simulation.

Only the simulator owns the target route and ground truth. Callers acting as
agents should use self_state(), observe(), set_waypoint() and try_tag().
An HTTP adapter must authenticate the caller before mapping it to agent_id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot, isfinite
from typing import Any


Point = tuple[float, float]
ROLES = {"observer", "leader", "marker"}
CARRIERS = {"UAV", "USV", "UUV"}
EPS = 1e-9


def distance(a: Point, b: Point) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


def _point(value: Any, label: str) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be a two-coordinate point")
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain numbers") from exc
    if not (isfinite(x) and isfinite(y)):
        raise ValueError(f"{label} must contain finite coordinates")
    return (x, y)


@dataclass(frozen=True)
class SimConfig:
    width: float = 100.0
    height: float = 100.0
    dt: float = 1.0
    agent_speed: float = 2.0
    target_speed: float = 1.0
    observation_radius: float = 25.0
    observation_period: int = 5
    support_radius: float = 15.0
    marker_radius: float = 5.0
    hold_seconds: int = 3
    max_time: int = 120

    def __post_init__(self) -> None:
        positive = (
            "width", "height", "dt", "agent_speed", "target_speed",
            "observation_radius", "support_radius", "marker_radius",
        )
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("observation_period", "hold_seconds", "max_time"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.dt != 1.0:
            raise ValueError("the first version uses one-second simulation ticks")


@dataclass
class AgentState:
    agent_id: str
    carrier_type: str
    role: str
    pos: Point
    waypoint: Point | None = None

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("agent_id must not be empty")
        if self.carrier_type not in CARRIERS:
            raise ValueError(f"invalid carrier type: {self.carrier_type}")
        if self.role not in ROLES:
            raise ValueError(f"invalid role: {self.role}")
        self.pos = _point(self.pos, f"{self.agent_id}.pos")
        if self.waypoint is not None:
            self.waypoint = _point(self.waypoint, f"{self.agent_id}.waypoint")


@dataclass
class CaptureEnvironment:
    config: SimConfig
    agents: list[AgentState]
    target_route: list[Point]
    scenario_id: str = "unnamed"
    team_selection: dict[str, Any] | None = None
    time: int = field(init=False, default=0)
    status: str = field(init=False, default="running")
    capture_time: int | None = field(init=False, default=None)
    events: list[dict[str, Any]] = field(init=False, default_factory=list)
    trace: list[dict[str, Any]] = field(init=False, default_factory=list)
    _agents: dict[str, AgentState] = field(init=False, repr=False)
    _target_pos: Point = field(init=False, repr=False)
    _observations: dict[str, dict[str, Any]] = field(
        init=False, repr=False, default_factory=dict
    )
    _support_streak: dict[str, int] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        self._agents = {agent.agent_id: agent for agent in self.agents}
        if len(self._agents) != len(self.agents):
            raise ValueError("agent_id values must be unique")
        counts = {role: sum(a.role == role for a in self.agents) for role in ROLES}
        if counts["observer"] < 1 or counts["leader"] != 1 or counts["marker"] < 1:
            raise ValueError("need an observer, exactly one leader, and a marker")
        for agent in self.agents:
            if not self._inside(agent.pos):
                raise ValueError(f"{agent.agent_id} starts outside the map")
            if agent.waypoint is not None and not self._inside(agent.waypoint):
                raise ValueError(f"{agent.agent_id} waypoint is outside the map")
        self.target_route = [
            _point(point, f"target_route[{i}]")
            for i, point in enumerate(self.target_route)
        ]
        self._validate_route()
        self._target_pos = self.target_route[0]
        if not any(
            distance(a.pos, self._target_pos) <= self.config.observation_radius + EPS
            for a in self.agents if a.role == "observer"
        ):
            raise ValueError("at least one observer must discover the target at t=0")
        self._support_streak = {
            a.agent_id: 0 for a in self.agents if a.role == "marker"
        }
        self._sample_observations()
        self._record_trace()
        self.events.append({"time": 0, "type": "initial_discovery_and_team_ready"})

    def _inside(self, pos: Point) -> bool:
        return (
            -EPS <= pos[0] <= self.config.width + EPS
            and -EPS <= pos[1] <= self.config.height + EPS
        )

    def _on_boundary(self, pos: Point) -> bool:
        return self._inside(pos) and (
            abs(pos[0]) <= EPS
            or abs(pos[0] - self.config.width) <= EPS
            or abs(pos[1]) <= EPS
            or abs(pos[1] - self.config.height) <= EPS
        )

    def _validate_route(self) -> None:
        route = self.target_route
        if len(route) < 3:
            raise ValueError("target_route must contain a turn")
        if not self._on_boundary(route[0]):
            raise ValueError("target must enter at the map boundary")
        if self._inside(route[-1]):
            raise ValueError("target route must end outside the map")
        if any(not self._inside(pos) for pos in route[1:-1]):
            raise ValueError("intermediate target waypoints must stay inside the map")
        vectors: list[Point] = []
        for start, end in zip(route, route[1:]):
            if distance(start, end) <= EPS:
                raise ValueError("target route contains a zero-length segment")
            vectors.append((end[0] - start[0], end[1] - start[1]))
        if not any(
            abs(a[0] * b[1] - a[1] * b[0]) > EPS
            for a, b in zip(vectors, vectors[1:])
        ):
            raise ValueError("target route must change direction")
        # The final leg must cross the boundary rather than jump from an
        # interior point straight to an outside endpoint.
        final_inside = route[-2]
        final_outside = route[-1]
        if not self._inside(final_inside) or self._inside(final_outside):
            raise ValueError("invalid exit segment")

    def _position_on_route(self, elapsed_seconds: float) -> Point:
        remaining = elapsed_seconds * self.config.target_speed
        for start, end in zip(self.target_route, self.target_route[1:]):
            length = distance(start, end)
            if remaining <= length:
                ratio = remaining / length
                return (
                    start[0] + ratio * (end[0] - start[0]),
                    start[1] + ratio * (end[1] - start[1]),
                )
            remaining -= length
        return self.target_route[-1]

    def _sample_observations(self) -> None:
        for agent in self.agents:
            if agent.role != "observer":
                continue
            visible = distance(agent.pos, self._target_pos) <= (
                self.config.observation_radius + EPS
            )
            self._observations[agent.agent_id] = {
                "observer_id": agent.agent_id,
                "observed_at": self.time,
                "target_position": list(self._target_pos) if visible else None,
            }

    def _record_trace(self) -> None:
        self.trace.append({
            "time": self.time,
            "target_position": list(self._target_pos),
            "agent_positions": {
                agent.agent_id: list(agent.pos) for agent in self.agents
            },
            "support_streaks": dict(self._support_streak),
        })

    def _require_agent(self, agent_id: str) -> AgentState:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise ValueError(f"unknown agent_id: {agent_id}") from exc

    def _require_running(self) -> None:
        if self.status != "running":
            raise RuntimeError(f"episode already ended: {self.status}")

    def self_state(self, agent_id: str) -> dict[str, Any]:
        """Return only this agent's position, role, and active waypoint."""
        agent = self._require_agent(agent_id)
        return {
            "time": self.time,
            "agent_id": agent.agent_id,
            "carrier_type": agent.carrier_type,
            "role": agent.role,
            "position": list(agent.pos),
            "waypoint": list(agent.waypoint) if agent.waypoint else None,
        }

    def observe(self, agent_id: str) -> dict[str, Any]:
        """Return the latest local sample; repeated calls cannot refresh it."""
        agent = self._require_agent(agent_id)
        if agent.role != "observer":
            raise PermissionError("only observer agents can sample the target")
        sample = self._observations[agent_id]
        return {
            "observer_id": sample["observer_id"],
            "observed_at": sample["observed_at"],
            "target_position": (
                list(sample["target_position"])
                if sample["target_position"] is not None else None
            ),
        }

    def set_waypoint(self, agent_id: str, point: Point) -> dict[str, Any]:
        self._require_running()
        agent = self._require_agent(agent_id)
        point = _point(point, "waypoint")
        if not self._inside(point):
            raise ValueError("agent waypoint must stay inside the map")
        agent.waypoint = point
        self.events.append({
            "time": self.time, "type": "waypoint_set",
            "agent_id": agent_id, "point": list(point),
        })
        return {"accepted": True, "time": self.time, "waypoint": list(point)}

    def _update_support(self) -> None:
        observer_near = any(
            distance(a.pos, self._target_pos) <= self.config.support_radius + EPS
            for a in self.agents if a.role == "observer"
        )
        leader_near = any(
            distance(a.pos, self._target_pos) <= self.config.support_radius + EPS
            for a in self.agents if a.role == "leader"
        )
        for marker in (a for a in self.agents if a.role == "marker"):
            marker_near = (
                distance(marker.pos, self._target_pos)
                <= self.config.marker_radius + EPS
            )
            if observer_near and leader_near and marker_near:
                self._support_streak[marker.agent_id] += 1
            else:
                self._support_streak[marker.agent_id] = 0

    def try_tag(self, agent_id: str) -> dict[str, Any]:
        """A local marker request succeeds only after sustained joint support."""
        self._require_running()
        agent = self._require_agent(agent_id)
        if agent.role != "marker":
            raise PermissionError("only marker agents can request a tag")
        accepted = self._support_streak[agent_id] >= self.config.hold_seconds
        self.events.append({
            "time": self.time, "type": "tag_request",
            "agent_id": agent_id, "accepted": accepted,
            "reason": "accepted" if accepted else "support_not_held",
        })
        if accepted:
            self.status = "captured"
            self.capture_time = self.time
        return {"accepted": accepted, "time": self.time}

    def advance_one_second(self) -> None:
        self._require_running()
        if self.time >= self.config.max_time:
            raise RuntimeError("call finish_current_time() at the time limit")
        for agent in self.agents:
            if agent.waypoint is None:
                continue
            remaining = distance(agent.pos, agent.waypoint)
            if remaining <= EPS:
                agent.pos = agent.waypoint
                agent.waypoint = None
                continue
            travel = min(self.config.agent_speed * self.config.dt, remaining)
            fraction = travel / remaining
            agent.pos = (
                agent.pos[0] + fraction * (agent.waypoint[0] - agent.pos[0]),
                agent.pos[1] + fraction * (agent.waypoint[1] - agent.pos[1]),
            )
            if travel >= remaining - EPS:
                agent.pos = agent.waypoint
                agent.waypoint = None
        self.time += 1
        self._target_pos = self._position_on_route(self.time * self.config.dt)
        if self.time % self.config.observation_period == 0:
            self._sample_observations()
        if self._inside(self._target_pos):
            self._update_support()
        else:
            self.status = "escaped"
            self.events.append({"time": self.time, "type": "target_escaped"})
        self._record_trace()

    def finish_current_time(self) -> None:
        """Call after all t=max_time actions, so a final valid tag can count."""
        if self.status == "running" and self.time >= self.config.max_time:
            self.status = "timeout"
            self.events.append({"time": self.time, "type": "timeout"})

    def result(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "time": self.time,
            "capture_time": self.capture_time,
            "events": [dict(event) for event in self.events],
            "trace": [dict(frame) for frame in self.trace],
            "team_selection": self.team_selection,
        }
