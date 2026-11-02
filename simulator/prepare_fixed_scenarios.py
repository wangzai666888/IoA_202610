"""Prepare isolated five-client bundles for saved scenes, preserving model settings."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets

from simulator.fixed_scenarios import ROOT, COUNT, fixed_scene, frozen_write, json_text
from simulator.service import validate_auth

RUNTIME_DIR = Path("simulator/fixed_runtime")
CONFIG_DIR = Path("configs/client_configs/cases/fixed_scenes.local")
TEMPLATE_DIR = Path("configs/client_configs/cases/capture_five.local")


def read_templates(root):
    by_name, by_role = {}, {}
    for path in sorted((root / TEMPLATE_DIR).glob("*.yaml")):
        data = json.loads(path.read_text(encoding="utf-8"))
        tool = data["tool_agent"]
        role = Path(tool["tools_config"]).stem
        if role not in {"observer", "leader", "marker"} or tool["agent_type"] != "ReAct":
            raise ValueError(f"invalid role/tool template: {path}")
        if tool["agent_name"] != data["comm"]["name"]:
            raise ValueError(f"template identity mismatch: {path}")
        by_name[tool["agent_name"]] = data
        by_role.setdefault(role, data)
    if set(by_role) != {"observer", "leader", "marker"}:
        raise ValueError("need observer, leader and marker templates in capture_five.local")
    return by_name, by_role


def bind(source, target, *, readonly=False):
    return {"type": "bind", "source": str(source), "target": target,
            "read_only": readonly, "bind": {"create_host_path": False}}


def prepare_scene(number, *, root=ROOT):
    root = Path(root).resolve()
    scenario, env, record = fixed_scene(number, root)
    directory = root / RUNTIME_DIR / f"scene_{number:02d}"
    by_name, by_role = read_templates(root)
    members = record["members"]
    auth_path = directory / "auth.local.json"
    auth = (json.loads(auth_path.read_text()) if auth_path.exists() else
            {"controller_token": secrets.token_urlsafe(32),
             "agent_tokens": {m["id"]: secrets.token_urlsafe(32) for m in members}})
    validate_auth(auth, {m["id"] for m in members})
    descriptions = {
        "observer": "观测者。按本轮动作合同观测目标或设置本地跟随航点，报告真实位置或未见结果及原 observed_at。",
        "leader": "唯一队长。只使用实际交付的观测，区分最新采样和历史位置，为自己、标记者和需要引导的观测者规划航点，不预测路线。",
        "marker": "标记者。执行队长的有效航点及本轮标记合同，报告真实 accepted；false 是正常结果，already_captured 表示其他标记者已成功。",
    }
    common = "只处理任务合同指定的 episode_id 和 expected_time；工具仅操作自己，不推进时钟，不读取全局真值。"
    files, private, clients, config_paths = {}, set(), {}, {}
    output = root / "simulator/outputs" / f"scene_{number:02d}"
    services = {
        "simulator": {
            "image": "ioa-simulator:five", "container_name": "ioa-fixed-simulator",
            "command": ["python", "-m", "simulator.service", "--host", "0.0.0.0",
                        "--scenario", "/run/fixed/scene.json", "--auth-file", "/run/fixed/auth.json",
                        "--output-dir", "/app/outputs"],
            "ports": ["127.0.0.1:8090:8090"],
            "volumes": [bind(scenario, "/run/fixed/scene.json", readonly=True),
                        bind(auth_path, "/run/fixed/auth.json", readonly=True), bind(output, "/app/outputs"),
                        bind(root / "simulator/replay_template.html",
                             "/app/simulator/replay_template.html", readonly=True)],
            "healthcheck": {"test": ["CMD", "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=2).read()"],
                "interval": "5s", "timeout": "3s", "retries": 6},
        }
    }
    for index, member in enumerate(members):
        name, role = member["id"], member["role"]
        data = deepcopy(by_name.get(name, by_role[role]))
        data["comm"].update(name=name, desc=descriptions[role] + common)
        data["tool_agent"].update(agent_name=name, desc=descriptions[role] + common,
            container_name=f"ioa-fixed-slot-{index}-tool", tools_config=f"/app/simulator/tool_configs/{role}.json")
        # Five stable slot names prevent accumulating 50 running tool containers.
        # ReAct creation replaces the old slot tool with this scene's identity/token.
        config = root / CONFIG_DIR / f"scene_{number:02d}" / f"{name}.yaml"
        files[config] = json_text(data)
        config_paths[name] = config.relative_to(root).as_posix()
        credential = directory / f"{name}.env"
        files[credential] = "SIM_URL=http://ioa-fixed-simulator:8090\nSIM_AGENT_TOKEN=" + auth["agent_tokens"][name] + "\n"
        private.add(credential)
        clients[name] = f"http://127.0.0.1:{5070 + index}"
        database = directory / "database" / name
        services[f"slot-{index}"] = {
            "image": "ioa-client:sim", "container_name": f"ioa-fixed-slot-{index}-client",
            "env_file": [str(root / "dockerfiles/compose/.env"), str(credential)],
            "environment": {"CUSTOM_CONFIG": "configs/" + config.relative_to(root / "configs/client_configs").as_posix()},
            "volumes": [bind(Path("/var/run/docker.sock"), "/var/run/docker.sock"),
                        bind(database, "/app/database"), bind(root / "configs/client_configs", "/app/configs", readonly=True)],
            "ports": [f"127.0.0.1:{5070 + index}:5050"],
            "depends_on": {"simulator": {"condition": "service_healthy"}},
            "stop_grace_period": "30s",
            "healthcheck": {"test": ["CMD", "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/openapi.json',timeout=3).read()"],
                "interval": "10s", "timeout": "5s", "retries": 12, "start_period": "30s"},
        }
    manifest = {"bundle_version": 1, "scenario_id": env.scenario_id, "scenario_sha256": env.scenario_sha256,
                "scenario_file": scenario.relative_to(root).as_posix(), "members": members,
                "clients": clients, "client_configs": config_paths}
    files[directory / "clients.json"] = json_text(manifest)
    files[directory / "pairs.json"] = json_text(record["pairs"])
    files[directory / "selection.json"] = json_text(env.team_selection)
    files[directory / "compose.yaml"] = json_text({"services": services,
        "networks": {"default": {"name": "agent_network", "external": True}}})
    # Keep credentials out of public snapshots and create with restrictive mode.
    for path, content in files.items():
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"{path} differs; preserve the existing bundle before regenerating")
    directory.mkdir(parents=True, exist_ok=True)
    if not auth_path.exists():
        fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json_text(auth))
    for path in private:
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(files[path])
    frozen_write(files)
    for path in private | {auth_path}:
        path.chmod(0o600)
    output.mkdir(parents=True, exist_ok=True)
    for member in members:
        (directory / "database" / member["id"]).mkdir(parents=True, exist_ok=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=int, choices=range(1, COUNT + 1))
    args = parser.parse_args()
    for number in ([args.scene] if args.scene else range(1, COUNT + 1)):
        result = prepare_scene(number)
        print(f"scene {number:02d}: {result['scenario_id']}; selected="
              + ", ".join(m["id"] for m in result["members"]))
    print("Prepared only; no containers or paid model calls were started.")


if __name__ == "__main__":
    main()
