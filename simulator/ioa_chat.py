"""Export the IoA Server's group messages after a simulation episode.

This records messages sent through the group chat. Private model prompts and
tool-agent reasoning remain in their own container logs.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen


MESSAGE_TYPES = {
    0: "default", 1: "proposal", 2: "vote", 3: "voting_result",
    4: "discussion", 5: "sync_task_assignment", 6: "async_task_assignment",
    7: "inform_task_result", 8: "inform_task_progress", 9: "pause",
    10: "conclude_group_discussion", 11: "conclusion",
}


def fetch_chat_record(server_url: str, comm_id: str, timeout: float = 30) -> dict:
    """Fetch one conversation, without fetching every session on the server."""
    request = Request(
        server_url.rstrip("/") + "/fetch_chat_record",
        data=json.dumps({"comm_id": comm_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or comm_id not in payload:
        raise ValueError(f"IoA Server returned no chat record for {comm_id}")
    record = payload[comm_id]
    if not isinstance(record, dict) or record.get("comm_id") != comm_id:
        raise ValueError("IoA Server returned a chat record for another session")
    messages = record.get("chat_record")
    if not isinstance(messages, list) or not messages:
        raise ValueError("IoA Server returned an empty or invalid chat record")
    for number, message in enumerate(messages, 1):
        if (not isinstance(message, dict) or message.get("comm_id") != comm_id
                or not isinstance(message.get("sender"), str)
                or not isinstance(message.get("content"), str)):
            raise ValueError(f"invalid IoA chat message #{number}")
    return payload


def format_chat_transcript(comm_id: str, record: dict) -> str:
    """Present the server's original message order with simulation round labels."""
    messages = record["chat_record"]
    lines = [f"IoA group conversation: {comm_id}",
             f"Agents: {', '.join(record.get('agent_names', []))}",
             f"Messages: {len(messages)}", ""]
    previous_round = None
    for number, message in enumerate(messages, 1):
        round_id = message.get("proposal_id") or "initialization / no round ID"
        if round_id != previous_round:
            lines.extend([f"=== {round_id} ===", ""])
            previous_round = round_id
        message_type = message.get("type")
        if isinstance(message_type, int):
            message_type = MESSAGE_TYPES.get(message_type, str(message_type))
        lines.append(f"[{number:03d}] {message['sender']} | {message_type}")
        lines.append(message["content"])
        for field, label in (("task_desc", "Assigned task"),
                             ("task_conclusion", "Task result")):
            value = message.get(field)
            if isinstance(value, str) and value and value not in message["content"]:
                lines.append(f"{label}: {value}")
        lines.append("")
    return "\n".join(lines) + "\n"


def attach_chat_artifacts(
    report: dict, server_url: str, output_dir: Path,
    fetch=fetch_chat_record,
) -> dict:
    """Add conversation files to a finished episode without changing its outcome."""
    report["chat_export_error"] = None
    report["chat_message_count"] = None
    comm_id = report.get("comm_id")
    if not comm_id:
        report["chat_export_error"] = "No IoA communication session was created"
        return report
    try:
        payload = fetch(server_url, comm_id)
        record = payload[comm_id]
        transcript = format_chat_transcript(comm_id, record)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = report["episode_id"] + ".ioa-chat"
        json_path = output_dir / (stem + ".json")
        txt_path = output_dir / (stem + ".txt")
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        txt_path.write_text(transcript, encoding="utf-8")
        report["artifacts"]["ioa_chat_json"] = json_path.name
        report["artifacts"]["ioa_chat_txt"] = txt_path.name
        report["chat_message_count"] = len(record["chat_record"])
    except Exception as exc:
        report["chat_export_error"] = f"{type(exc).__name__}: {exc}"
    return report
