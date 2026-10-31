"""Load a frozen, inspectable scenario for the standalone simulator."""

from __future__ import annotations

import json
from pathlib import Path

from simulator.core import AgentState, CaptureEnvironment, SimConfig, _point
from simulator.team_selection import select_fixed_team


def load_scenario(path: str | Path) -> CaptureEnvironment:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("scenario root must be a JSON object")
    try:
        config = SimConfig(**data.get("config", {}))
        if "candidates" in data and "agents" in data:
            raise ValueError("use either candidates with selection, or an already fixed agents list")
        rows = data["candidates"] if "candidates" in data else data["agents"]
        agents = [
            AgentState(
                agent_id=row["id"],
                carrier_type=row["carrier_type"],
                role=row["role"],
                pos=row["position"],
            )
            for row in rows
        ]
        selection = None
        if "candidates" in data:
            if len(agents) != 9:
                raise ValueError("this candidate scenario requires exactly nine registered candidates")
            for agent in agents:
                if not (0 <= agent.pos[0] <= config.width and 0 <= agent.pos[1] <= config.height):
                    raise ValueError(f"candidate {agent.agent_id} starts outside the map")
            agents, selection = select_fixed_team(
                agents, _point(data["target"]["route"][0], "initial target position"),
                config.observation_radius, **data["selection"],
            )
            if len(agents) != 5:
                raise ValueError("the nine-candidate stage requires a five-member team")
        return CaptureEnvironment(
            config=config,
            agents=agents,
            target_route=data["target"]["route"],
            scenario_id=data.get("scenario_id", source.stem),
            team_selection=selection,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid scenario schema: {exc}") from exc
