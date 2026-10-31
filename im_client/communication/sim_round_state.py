"""Small, model-free helpers for one IoA discussion per simulation second.

The existing AgentMessage.proposal_id is unused by the capture configuration.
It carries an episode/time round key through the unmodified IoA server, which
re-serializes messages using the existing protocol. Only SIM_URL clients use it.
"""

from __future__ import annotations

import re
from .capture_state import begin_capture_round


ROUND_KEY = re.compile(r"^([0-9a-f]{32}):([0-9]{1,3})$")
PROMPT_HISTORY_ROUNDS = 6  # Current round plus five prior seconds (one observation interval).
CAPTURE_DISCUSSION_FIELDS = ("thought", "message_type", "next_people", "content")


def normalize_discussion_response(content):
    """Terminal messages do not route to a model-selected next speaker."""
    if (isinstance(content, dict)
            and content.get("message_type") == "conclude_group_discussion"
            and content.get("next_people") is None):
        return {**content, "next_people": []}
    return content


def discussion_response_error(content, allowed_types: set[str]) -> str | None:
    """Validate the model's discussion message before any field is consumed."""
    content = normalize_discussion_response(content)
    if not isinstance(content, dict):
        return "expected a JSON object"
    missing = [field for field in CAPTURE_DISCUSSION_FIELDS if field not in content]
    if missing:
        return "missing fields: " + ", ".join(missing)
    if not isinstance(content["thought"], str):
        return "thought must be a string"
    if not isinstance(content["message_type"], str) or content["message_type"] not in allowed_types:
        return "message_type is not a supported discussion type"
    speakers = content["next_people"]
    if not (isinstance(speakers, str)
            or isinstance(speakers, list) and all(isinstance(name, str) for name in speakers)):
        return "next_people must be a name or list of names"
    if not isinstance(content["content"], str) or not content["content"].strip():
        return "content must be a nonempty string"
    return None


async def generate_valid_discussion_response(
    llm, *, prepend_prompt, history, append_prompt, allowed_types: set[str],
    attempts: int = 2, on_invalid=None,
):
    """Retry only malformed capture replies; keep the same conversation state."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    base_append = [append_prompt] if isinstance(append_prompt, str) else list(append_prompt)
    for number in range(1, attempts + 1):
        current_append = list(base_append)
        if number > 1:
            current_append.append(
                f"上一条回复的具体格式错误：{error}。{shape}。只输出 JSON 对象，必须包含 "
                "thought、message_type、next_people、content；"
                "thought 为字符串；message_type 使用已有协作消息类型；content 必须为非空字符串。"
                "discussion、pause 的 next_people 填下一位成员的名称字符串；"
                "sync_task_assign、async_task_assign 填任务负责人的名称字符串列表；"
                "conclude_group_discussion 填空列表 []。不要把 next_people 写成对象、数字或 null。"
            )
        reply = await llm.agenerate_response(
            prepend_prompt=prepend_prompt, history=history,
            append_prompt=current_append, response_format={"type": "json_object"},
        )
        normalized = normalize_discussion_response(reply.content)
        error = discussion_response_error(normalized, allowed_types)
        if error is None:
            reply.content = normalized
            return reply
        payload = reply.content if isinstance(reply.content, dict) else {}
        kind = payload.get("message_type")
        kind = kind if isinstance(kind, str) and kind in allowed_types else "<invalid>"
        speaker_type = type(payload["next_people"]).__name__ if "next_people" in payload else "missing"
        shape = f"message_type={kind}, next_people_type={speaker_type}"
        if on_invalid is not None:
            on_invalid(number, error)
    raise ValueError(f"capture discussion response invalid after {attempts} attempts: {error}; {shape}")


def parse_round_id(round_id: str) -> tuple[str, int]:
    match = ROUND_KEY.fullmatch(round_id)
    if match is None:
        raise ValueError("sim_round_id must be '<32-character episode_id>:<simulation second>'")
    return match.group(1), int(match.group(2))


def begin_round(comm_info, task_manager, round_id: str, max_turns: int, goal: str) -> None:
    episode_id, time = parse_round_id(round_id)
    if type(max_turns) is not int or max_turns < 1:
        raise ValueError("max_turns must be a positive integer for a simulation round")
    comm_info.sim_capture_state = begin_capture_round(
        goal, round_id, getattr(comm_info, "sim_capture_state", None),
    )
    comm_info.sim_round_id = round_id
    comm_info.sim_episode_id = episode_id
    comm_info.sim_time = time
    comm_info.sim_round_message_start = len(comm_info.memory.messages)
    starts = list(comm_info.sim_round_message_starts)
    starts.append(comm_info.sim_round_message_start)
    comm_info.sim_round_message_starts = starts
    comm_info.sim_prompt_message_start = starts[max(0, len(starts) - PROMPT_HISTORY_ROUNDS)]
    comm_info.sim_round_task_start = task_manager.global_index
    comm_info.max_turns = max_turns
    comm_info.goal = goal
    comm_info.conclusion = None
    comm_info.sim_round_error = None


def round_message_count(comm_info) -> int:
    return max(0, len(comm_info.memory.messages) - comm_info.sim_round_message_start)


def round_status(comm_info, task_manager) -> dict:
    entries = [
        {
            "task_id": task.task_id,
            "assignee": task.assignee,
            "status": task.status.name,
        }
        for index, task in task_manager.tasks.values()
        if index >= comm_info.sim_round_task_start
    ]
    pending = [
        entry for entry in entries
        if entry["status"] in ("TO_START", "IN_PROGRESS")
    ]
    failed = [entry for entry in entries if entry["status"] == "FAILED"]
    return {
        "comm_id": comm_info.comm_id,
        "round_id": comm_info.sim_round_id,
        "episode_id": comm_info.sim_episode_id,
        "sim_time": comm_info.sim_time,
        "concluded": comm_info.conclusion is not None,
        "message_count": round_message_count(comm_info),
        "total_message_count": len(comm_info.memory.messages),
        "prompt_message_count": len(comm_info.memory.messages) - comm_info.sim_prompt_message_start,
        "max_turns": comm_info.max_turns,
        "round_error": comm_info.sim_round_error,
        "task_count": len(entries),
        "pending_tasks": pending,
        "failed_tasks": failed,
        "await_agents": sorted(task_manager.task_assign_manager.await_agents),
    }
