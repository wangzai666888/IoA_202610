"""Small capture-only task/report helpers; no model calls or simulator access."""

import json
from common.capture_protocol import TASK_PREFIX, validate_contract
from .capture_state import decode_batch


CAPTURE_COORDINATION_PROMPT = """本仿真使用简短、可直接执行的任务协议：
1. 角色描述中的“必须设置自身航点”指初始化或确需改变航点，不是每秒义务。
   包括队长在内，上一航点不变时继续执行，到达后停留；不要分配重复 sim_move_to，
   也不要为维持航点、等待、查询或总结创建工具任务。只有实际需要改变目的地时才重新分配。
2. 采样轮先给全部观测者分配观测任务并等回执，再协调需要改变的航点，
   等本批航点回执齐全后，给全部标记者分配本秒标记任务。t=0 全员须设置初始航点。
   同阶段成员可在一个 sync_task_assign 的 next_people 列表中同时分配。
3. content 必须直接写明每个接收者本次要调用的工具和完整参数：
   sim_move_to 写 x、y 及来自已收到观测的 observed_at；sim_observe、sim_try_tag 无参数。
   不要只说“按之前安排执行”；任务不会再经过模型转述或读取历史。
   工具后端只执行当前成员的本次动作，不能替你协调其他成员。
   不在本批任务中夹带未来阶段的动作，不把标记和航点更新混在同一批。
4. thought 用一句话；content 只写本次分工、坐标或必要异常，不复述角色规则和历史回执。
   工具结果已有执行器原始回执，不重写或编造回执。accepted=false 不重试。
   等本批真实结果返回再继续；本轮所需任务完成后结束讨论，结束消息只写一句话。
"""


def capture_assignment(agent_name, round_id, message, phase_state=None):
    """Forward the delivered assignment, never infer a task from old history."""
    if message is None or message.proposal_id != round_id:
        raise ValueError("capture assignment must belong to the current round")
    receivers = message.next_speaker
    receivers = [receivers] if isinstance(receivers, str) else receivers
    if not isinstance(receivers, list) or agent_name not in receivers:
        raise ValueError("capture assignment is not addressed to this agent")
    if not isinstance(message.content, str) or not message.content.strip():
        raise ValueError("capture assignment must contain an explicit task")
    if phase_state is not None:
        batch = decode_batch(message.content, message.sender)
        tasks = [t for t in batch["tasks"] if t["agent_id"] == agent_name]
        if len(tasks) != 1 or phase_state["pending"].get(agent_name) != tasks[0]:
            raise ValueError("capture assignment is not the registered pending contract")
        task = validate_contract(tasks[0], round_id, agent_name)
        return (None, TASK_PREFIX + json.dumps(task, ensure_ascii=False),
                f"{agent_name} {task['tool']}", "", "必须返回指定工具及参数的真实回执")
    description = (
        f"当前执行者：{agent_name}；绑定轮次：{round_id}。\n"
        "只执行下面这条已送达任务中分配给自己的本次工具动作。"
        "忽略其他成员的动作和后续阶段说明；不组织讨论、不分配任务。"
        "只使用本条指令明确给出的参数，缺失时报告失败，不从角色职责或旧任务猜测。\n"
        f"分配者：{message.sender}\n{message.content}"
    )
    return None, description, f"{agent_name} 本轮工具任务", "", "以本任务真实工具回执为准"


def capture_round_conclusion(status):
    """Close discussion without asserting that the controller's audit passed."""
    return json.dumps({
        "round_id": status["round_id"],
        "discussion_closed": True,
        "task_count": status["task_count"],
        "pending_tasks": status["pending_tasks"],
        "failed_tasks": status["failed_tasks"],
        "await_agents": status["await_agents"],
        "audit": "pending_controller_verification",
    }, ensure_ascii=False)
