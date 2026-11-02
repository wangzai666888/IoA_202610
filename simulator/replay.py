"""Export a completed episode as a portable animation and replay record.

This is an evaluator-side output. Do not expose it to agents during a run:
the full trace and the preplanned target route contain ground truth.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simulator.core import CaptureEnvironment


TEMPLATE = Path(__file__).with_name("replay_template.html")


def build_replay_record(env: CaptureEnvironment) -> dict[str, Any]:
    if env.status == "running":
        raise ValueError("finish the episode before exporting a replay")
    result = env.result()
    return {
        "schema_version": 1,
        "scenario_id": result["scenario_id"],
        "scenario_sha256": result["scenario_sha256"],
        "generation": result["generation"],
        "experiment_rules": result["experiment_rules"],
        "status": result["status"],
        "capture_time": result["capture_time"],
        "end_time": result["time"],
        "config": asdict(env.config),
        "agents": [
            {
                "id": agent.agent_id,
                "carrier_type": agent.carrier_type,
                "role": agent.role,
            }
            for agent in env.agents
        ],
        "target_route": [list(point) for point in env.target_route],
        "trace": result["trace"],
        "events": result["events"],
        "team_selection": result["team_selection"],
    }


def render_replay_html(record: dict[str, Any]) -> str:
    """Render a replay from its saved record, without rerunning the simulation."""
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    # An untrusted scenario ID must not terminate an inline script tag.
    safe_payload = (
        payload.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    template = TEMPLATE.read_text(encoding="utf-8")
    marker = "/*__REPLAY_DATA__*/"
    if template.count(marker) != 1:
        raise RuntimeError("replay template must contain exactly one data marker")
    return template.replace(marker, safe_payload)


def write_replay(env: CaptureEnvironment, html_path: str | Path) -> tuple[Path, Path]:
    """Write self-contained HTML and its machine-readable JSON companion."""
    record = build_replay_record(env)
    html_path = Path(html_path)
    if html_path.suffix.lower() != ".html":
        raise ValueError("replay output path must end in .html")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = html_path.with_suffix(".json")
    html = render_replay_html(record)
    json_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    html_path.write_text(html, encoding="utf-8")
    return html_path, json_path
