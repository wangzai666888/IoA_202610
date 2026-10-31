"""One deterministic smoke episode; this policy does not use IoA or an LLM.

The fixture has ideal instant relay of a legal observer sample. It validates
the simulator mechanics, not communication-window or Agent performance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from simulator.core import CaptureEnvironment
from simulator.replay import write_replay
from simulator.scenario import load_scenario


DEFAULT_SCENARIO = Path(__file__).parent / "scenarios" / "pilot_success.json"


def _display_path(path: Path) -> str:
    absolute = path.resolve()
    try:
        return absolute.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(absolute)


def run_scripted_episode(env: CaptureEnvironment) -> dict:
    observers = [a.agent_id for a in env.agents if a.role == "observer"]
    markers = [a.agent_id for a in env.agents if a.role == "marker"]

    while env.status == "running":
        if env.time % env.config.observation_period == 0:
            # Only use the observer's permitted, time-stamped local sample.
            samples = [env.observe(agent_id) for agent_id in observers]
            visible = [
                sample for sample in samples
                if sample["target_position"] is not None
            ]
            if visible:
                newest = max(visible, key=lambda sample: sample["observed_at"])
                for agent in env.agents:
                    env.set_waypoint(agent.agent_id, newest["target_position"])
        for marker_id in markers:
            env.try_tag(marker_id)
            if env.status != "running":
                break
        if env.status != "running":
            break
        if env.time == env.config.max_time:
            env.finish_current_time()
        else:
            env.advance_one_second()
    return env.result()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument(
        "--full-trace", action="store_true",
        help="print evaluator-only positions and event log",
    )
    parser.add_argument(
        "--replay-output", type=Path,
        help="HTML replay path (default: simulator/outputs/<scenario_id>.html)",
    )
    parser.add_argument(
        "--no-replay", action="store_true",
        help="print the result without writing HTML and JSON replay files",
    )
    args = parser.parse_args()
    env = load_scenario(args.scenario)
    result = run_scripted_episode(env)
    replay_paths = None
    if not args.no_replay:
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", env.scenario_id).strip("_")
        default_path = Path(__file__).parent / "outputs" / f"{safe_id or 'episode'}.html"
        replay_paths = write_replay(env, args.replay_output or default_path)
    if not args.full_trace:
        result = {
            "scenario_id": result["scenario_id"],
            "status": result["status"],
            "time": result["time"],
            "capture_time": result["capture_time"],
            "trace_frames": len(result["trace"]),
            "event_count": len(result["events"]),
        }
    if replay_paths is not None:
        result["replay_html"] = _display_path(replay_paths[0])
        result["replay_json"] = _display_path(replay_paths[1])
    if env.team_selection is not None:
        result["selected_ids"] = env.team_selection["selected_ids"]
        result["candidate_count"] = env.team_selection["candidate_count"]
        if replay_paths is not None:
            path = replay_paths[0].with_suffix(".selection.json")
            path.write_text(json.dumps(env.team_selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["selection_json"] = _display_path(path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
