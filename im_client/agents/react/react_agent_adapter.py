import os
import uuid
from typing import Dict, List

import requests
from docker.models.containers import Container
from pydantic import BaseModel

from common.config import global_config
from common.types.llm import LLMResult
from common.log import logger

import httpx

from .. import AgentAdapter
from .capture_guard import render_capture_result
from common.capture_protocol import decode_task, decode_receipt_report


class TaskDesc(BaseModel):
    task_desc: str
    uid: str
    sim_round_id: str | None = None


class MemoryAdded(BaseModel):
    role: str
    message: str


class MemoryGot(BaseModel):
    responses: List[Dict]


class ReActAgent(AgentAdapter):
    """The adapter for ReAct agent. Adapting ReAct agent's interface to IoA's protocol"""

    def __init__(self, docker_container: Container, agent_name: str):
        self.docker_container = docker_container
        self.agent_name = agent_name
        self.task_desc = ""
        self.url = f"http://{global_config['tool_agent']['container_name']}:7070"

    async def run(self, task_desc: str, sim_round_id: str | None = None):
        uid = uuid.uuid4().hex
        if os.environ.get("SIM_URL") and sim_round_id is None:
            raise ValueError("capture tool task requires a simulation round")
        data = TaskDesc(task_desc=task_desc, uid=uid, sim_round_id=sim_round_id)
        headers = {"Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            response = await client.post(self.url + "/run", json=data.model_dump(), headers=headers)
        response.raise_for_status()
        if os.environ.get("SIM_URL"):
            result = render_capture_result(response.json(), uid, sim_round_id, self.agent_name)
            contract = decode_task(task_desc, sim_round_id, self.agent_name)
            if contract is not None:
                decode_receipt_report(result, contract)
            logger.info(f"Received verified capture response: {result}")
            return result
        logger.info(f"Received response: {response.text}")
        return response.text

    async def add_to_memory(self, memoryAdded: LLMResult):
        headers = {"Content-Type": "application/json"}
        data = MemoryAdded(role=memoryAdded.role, message=memoryAdded.content)
        requests.post(
            self.url + "/add_to_memory",
            json=data.model_dump(),
            headers=headers,
        )

    async def get_memory(self) -> list[LLMResult]:
        responses = requests.post(self.url + "/get_memory").json()
        messageList: list[LLMResult] = []
        for response in responses:
            llmResult = LLMResult(role=response["role"], content=response["message"])
            language = response.get("language", None)

            if language:
                llmResult.content += f"\n```{language}\n{response.get('code', None)}\n```"
            output = response.get("output", None)
            if output:
                llmResult.content += f"\noutput: {output}\n"
            messageList.append(llmResult)
        return messageList

    async def shutdown(self):
        self.docker_container.remove(force=True)
