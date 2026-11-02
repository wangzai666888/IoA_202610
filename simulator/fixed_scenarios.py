"""Generate and inspect ten frozen scenes; generation never runs a capture policy."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from math import dist
from pathlib import Path
import random

from common.communication_windows import pair_catalog
from simulator.core import EPS
from simulator.scenario import load_scenario, scenario_from_data

ROOT = Path(__file__).resolve().parents[1]
SCENE_DIR = Path("simulator/scenarios/fixed_10")
BASE_SEED = 20261031
COUNT = 10


def json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def scene_members(env):
    order = {"observer": 0, "leader": 1, "marker": 2}
    return [{"id": a.agent_id, "role": a.role, "carrier_type": a.carrier_type}
            for a in sorted(env.agents, key=lambda a: (order[a.role], a.agent_id))]


def frozen_write(files):
    """Idempotent generation; check all conflicts before writing any file."""
    for path, content in files.items():
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"{path} differs; frozen scenarios/configuration are not overwritten")
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)


def generate_suite(template: Path, output_dir: Path, *, base_seed=BASE_SEED, max_attempts=10000):
    raw = Path(template).read_bytes()
    base = json.loads(raw)
    base["selection"] = {"radius": None, "role_counts": {"observer": 2, "leader": 1, "marker": 2}}
    # Validate the template's rules before sampling. No capture/escape outcomes
    # or future target route are used to accept or reject an initial layout.
    initial = scenario_from_data(base)
    if initial.config.width != 100 or initial.config.height != 100 or len(base.get("candidates", [])) != 9:
        raise ValueError("fixed suite requires nine candidates on a 100 by 100 metre map")
    files, scenes = {}, []
    for number in range(1, COUNT + 1):
        seed = base_seed + number - 1
        rng = random.Random(seed)
        data = deepcopy(base)
        data["scenario_id"] = f"random_fixed_{number:02d}"
        for attempt in range(1, max_attempts + 1):
            # Every draw resamples ALL nine agents independently, with no
            # minimum spacing, forced identities, or hidden success filter.
            for row in data["candidates"]:
                row["position"] = [rng.uniform(0.0, 100.0), rng.uniform(0.0, 100.0)]
            if any(row["role"] == "observer" and dist(row["position"], data["target"]["route"][0])
                   <= initial.config.observation_radius + EPS for row in data["candidates"]):
                break
        else:
            raise ValueError(f"no initial discovery after {max_attempts} draws for scene {number}")
        data["generation"] = {
            "method": "independent_uniform_xy_then_initial_discovery_rejection",
            "rng": "python_random_MT19937", "seed": seed,
            "attempts": attempt, "rejected_no_initial_discovery": attempt - 1,
            "bounds": [0.0, 100.0], "minimum_separation": None,
            "template_sha256": hashlib.sha256(raw).hexdigest(),
        }
        env = scenario_from_data(data)
        members = scene_members(env)
        content = json_text(data)
        filename = f"scene_{number:02d}.json"
        files[Path(output_dir) / filename] = content
        scenes.append({"scene": number, "file": filename, "scenario_id": env.scenario_id,
                       "scenario_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                       "seed": seed, "attempts": attempt, "members": members,
                       "pairs": pair_catalog(members)})
    manifest = {"version": 1, "count": COUNT, "base_seed": base_seed,
                "acceptance": "initial_discovery_only; no spacing or capture-outcome screening",
                "scenes": scenes}
    files[Path(output_dir) / "manifest.json"] = json_text(manifest)
    frozen_write(files)
    return manifest


def fixed_scene(number, root=ROOT):
    if type(number) is not int or not 1 <= number <= COUNT:
        raise ValueError("scene must be an integer from 1 to 10")
    directory = Path(root) / SCENE_DIR
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("version") != 1 or manifest.get("count") != COUNT
            or [row["scene"] for row in manifest["scenes"]] != list(range(1, COUNT + 1))):
        raise ValueError("invalid frozen scene manifest")
    record = manifest["scenes"][number - 1]
    if record["file"] != f"scene_{number:02d}.json":
        raise ValueError("scene filename does not match its index")
    path = directory / record["file"]
    env = load_scenario(path)
    if env.scenario_id != record["scenario_id"] or env.scenario_sha256 != record["scenario_sha256"]:
        raise ValueError(f"scene {number} differs from its frozen ID/SHA-256 manifest")
    if (env.team_selection["radius"] is not None or not env.generation
            or env.generation["seed"] != record["seed"] or env.generation["attempts"] != record["attempts"]):
        raise ValueError(f"scene {number} generation/whole-map selection metadata mismatch")
    members = scene_members(env)
    if members != record["members"] or pair_catalog(members) != record["pairs"]:
        raise ValueError(f"scene {number} selected team/pairs differ from its frozen manifest")
    return path, env, record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("generate", "list", "verify"))
    parser.add_argument("--template", type=Path, default=ROOT / "simulator/scenarios/five_team.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / SCENE_DIR)
    parser.add_argument("--base-seed", type=int, default=BASE_SEED)
    args = parser.parse_args()
    if args.operation == "generate":
        manifest = generate_suite(args.template, args.output_dir, base_seed=args.base_seed)
        print(f"Frozen {len(manifest['scenes'])} scenes in {args.output_dir}")
        return
    for number in range(1, COUNT + 1):
        _, env, record = fixed_scene(number)
        print(f"{number:02d}  {env.scenario_id}  seed={record['seed']}  draws={record['attempts']}  "
              + ", ".join(m["id"] for m in record["members"]))
    if args.operation == "verify":
        print("All 10 scenario hashes, teams and pair mappings verified; no model calls.")


if __name__ == "__main__":
    main()
