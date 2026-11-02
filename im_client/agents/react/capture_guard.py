"""Evidence for a single capture tool task, populated only by the executor.

The minimum action check prevents a prose-only completion. It does not replace
the controller's role/operation/order audit for the whole simulation second.
"""

import json
import re
from common.capture_protocol import validate_contract, check_contract_receipt


PROTOCOL = "ioa-capture-tool-result-v1"
ACTION_TOOLS = frozenset({"sim_observe", "sim_move_to", "sim_try_tag"})
READ_TOOLS = frozenset({"sim_self_state"})
ROUND_ID = re.compile(r"([0-9a-f]{32}):([0-9]{1,3})\Z")


class CaptureTaskEvidence:
    def __init__(self, uid: str, sim_round_id: str, agent_name: str, contract=None):
        match = ROUND_ID.fullmatch(sim_round_id or "")
        if match is None:
            raise ValueError("capture evidence requires a valid simulation round")
        self.uid = uid
        self.sim_round_id = sim_round_id
        self.agent_name = agent_name
        self.episode_id, self.sim_time = match.group(1), int(match.group(2))
        self.receipts = []
        self.contract = validate_contract(contract, sim_round_id, agent_name) if contract is not None else None

    @property
    def has_action(self) -> bool:
        if self.contract and self.contract["tool"] == "sim_self_state":
            return any(row["tool"] == "sim_self_state" for row in self.receipts)
        return any(row["tool"] in ACTION_TOOLS for row in self.receipts)

    def check_call(self, tool, arguments):
        if self.contract is None or (tool == "sim_self_state" and self.contract["tool"] != tool):
            return
        if tool != self.contract["tool"]:
            raise ValueError(f"this task requires {self.contract['tool']}; {tool} is not assigned")
        if self.has_action:
            raise ValueError("assigned action already has a receipt; submit without repeating it")
        supplied = {k: v for k, v in arguments.items() if k not in {"episode_id", "expected_time"}}
        if supplied != self.contract["arguments"]:
            raise ValueError("tool arguments differ from assigned contract")

    def record(self, tool: str, raw_result: str) -> None:
        """Call only after the real HTTP tool returns, never with model text."""
        if tool not in ACTION_TOOLS | READ_TOOLS:
            raise ValueError(f"not a capture tool: {tool}")
        result = json.loads(raw_result)
        if self.contract is not None:
            check_contract_receipt(self.contract, tool, result)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError(f"{tool} did not return a successful tool response")
        if (result.get("episode_id") != self.episode_id
                or type(result.get("time")) is not int
                or result["time"] != self.sim_time):
            raise ValueError(f"{tool} response does not match the bound simulation round")
        if tool in {"sim_move_to", "sim_try_tag"}:
            if type(result.get("accepted")) is not bool:
                raise ValueError(f"{tool} response is missing boolean accepted")
            if tool == "sim_move_to" and not result["accepted"]:
                raise ValueError("sim_move_to was not accepted")
        if tool == "sim_observe":
            if (result.get("observer_id") != self.agent_name
                    or "observed_at" not in result or "target_position" not in result):
                raise ValueError("sim_observe response is missing observation fields")
        if tool == "sim_self_state" and result.get("agent_id") != self.agent_name:
            raise ValueError("sim_self_state response belongs to another agent")
        # accepted=false is a valid tag receipt; observing nothing is also valid.
        self.receipts.append({"tool": tool, "result": result})

    def finish(self, conclusion: str) -> dict:
        if not self.has_action:
            raise ValueError(
                "submit_task rejected: this task has no real sim_observe, sim_move_to "
                "or sim_try_tag receipt (or an explicitly assigned sim_self_state receipt). Execute the action assigned in THIS task "
                "before submitting; prose, sim_self_state and past tasks are not "
                "action evidence. Do not repeat an action that already returned a receipt."
            )
        if not isinstance(conclusion, str) or not conclusion.strip():
            raise ValueError("submit_task requires a nonempty conclusion")
        result = {
            "protocol": PROTOCOL, "uid": self.uid,
            "sim_round_id": self.sim_round_id, "agent_name": self.agent_name,
            "tool_receipts": self.receipts, "conclusion": conclusion,
        }
        if self.contract is not None:
            result["contract"] = self.contract
        return result


def render_capture_result(payload: dict, uid: str, sim_round_id: str, agent_name: str) -> str:
    """Validate the backend envelope and put unedited receipts before model prose."""
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise ValueError("capture backend returned no executor evidence; update the tool image")
    if (payload.get("uid") != uid or payload.get("sim_round_id") != sim_round_id
            or payload.get("agent_name") != agent_name):
        raise ValueError("capture backend evidence belongs to another task, round or agent")
    receipts = payload.get("tool_receipts")
    if not isinstance(receipts, list):
        raise ValueError("capture backend evidence has no receipt list")
    verified = CaptureTaskEvidence(uid, sim_round_id, agent_name, payload.get("contract"))
    for row in receipts:
        if not isinstance(row, dict) or set(row) != {"tool", "result"}:
            raise ValueError("invalid capture receipt entry")
        verified.record(row["tool"], json.dumps(row["result"], ensure_ascii=False))
    result = verified.finish(payload.get("conclusion"))
    facts = {key: value for key, value in result.items() if key != "conclusion"}
    return (
        "【执行器记录的真实工具回执】以下 JSON 由执行器生成，模型文字不能替代或补充回执。\n"
        + json.dumps(facts, ensure_ascii=False)
        + "\n【模型说明，仅供参考；完成情况以真实回执和仿真器审计为准】\n"
        # Bound only model prose; never truncate or summarize a real receipt.
        + (result["conclusion"] if len(result["conclusion"]) <= 240
           else result["conclusion"][:240] + "…（模型说明已缩略）")
    )
