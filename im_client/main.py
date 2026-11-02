from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import HTTPException, Request
import os
import secrets
from pymilvus import connections

from agents import AgentAdapter
from communication.communication_layer import CommunicationLayer
from common.config import global_config
from common.types import LaunchGoalParam
from common.types.communication import AgentMessage, CommunicationState, CommunicationType
from communication.window_capture import CaptureWindowSession
from common.communication_windows import PROTOCOL_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    global communicator
    if communicator is None:
        tool_agent = AgentAdapter.create(global_config["tool_agent"])
        comm_config = global_config["comm"]
        communicator = await CommunicationLayer.create(
            comm_config["name"],
            comm_config["desc"],
            comm_config["type"],
            tool_agent,
            comm_config["support_nested_teams"],
            discussion_only=comm_config["discussion_only"],
        )
    yield
    await communicator.shutdown()


app = FastAPI(lifespan=lifespan)

communicator: CommunicationLayer = None

connections.connect(
    alias="default",
    user="username",
    password="password",
    # host='localhost', #'host.docker.internal',
    host="standalone",
    port="19530",
)


@app.post("/launch_goal")
async def launch_goal(param: LaunchGoalParam):
    return await communicator.launch_goal(**param.model_dump())


@app.get("/simulation/round_status")
async def simulation_round_status(comm_id: str):
    if not os.environ.get("SIM_URL"):
        raise HTTPException(status_code=404, detail="simulation mode is disabled")
    try:
        return communicator.simulation_round_status(comm_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/simulation/window/capabilities")
async def window_capabilities():
    return {"protocol_version": PROTOCOL_VERSION}


@app.post("/simulation/window/init")
async def initialize_window(body: dict):
    if not os.environ.get("SIM_URL"):
        raise HTTPException(404, "simulation mode is disabled")
    comm_id = body["comm_id"]
    if (comm_id in communicator.window_sessions
            or any(not row["session"].closed for row in communicator.window_sessions.values())):
        raise HTTPException(409, "a window session already exists; close it or recreate clients")
    if not isinstance(body.get("key"), str) or len(body["key"]) < 24:
        raise HTTPException(400, "a private per-run control key is required")
    async def send(content, round_id):
        packet = AgentMessage(content=content, sender=communicator.name, comm_id=comm_id,
                              proposal_id=round_id, state=CommunicationState.DISCUSSION,
                              type=CommunicationType.DISCUSSION)
        await communicator.server_websocket.send_message(packet.model_dump_json())
    session = CaptureWindowSession(communicator.name, comm_id, body["episode_id"], body["members"],
        body["observation_period"], communicator.llm, communicator.tool_agent_run, send, profile=body["profile"])
    communicator.window_sessions[comm_id] = {"session": session, "key": body["key"]}
    return session.status()


@app.post("/simulation/window/control")
async def control_local_window(body: dict, request: Request):
    row = communicator.window_sessions.get(body["comm_id"])
    if row is None:
        raise HTTPException(404, "unknown local window session")
    key = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not secrets.compare_digest(key, row["key"]):
        raise HTTPException(403, "invalid window control key")
    session = row["session"]
    operation = body["operation"]
    if operation == "close":
        session.closed = True
        return session.status()
    if operation == "status":
        return session.status()
    if operation == "export":
        return {**session.status(), "events": session.events, "uplink_messages": session.uplink_messages}
    if operation != "tick":
        raise HTTPException(400, "unknown local window operation")
    try:
        return await session.tick(body["time"], body["phase"])
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5050)
