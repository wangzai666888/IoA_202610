"""Post-run collaboration timeline, derived solely from recorded evidence."""
import json
from common.communication_windows import MESSAGE_PREFIX


def authorization_timeline(transport, local_events, tool_calls, end_time):
    deliveries = (transport or {}).get("deliveries", [])
    admissions = {row["message_id"]: row for row in (transport or {}).get("messages", [])}
    result = []
    for local in local_events.values():
        for row in local.get("uplink_messages", []):
            packet = json.loads(row["content"][len(MESSAGE_PREFIX):])
            if packet["kind"] != "authorization":
                continue
            grant = packet["payload"]
            delivery = next((d for d in deliveries if d["message_id"] == row["message_id"]
                             and d["receiver"] == grant["marker_id"]), None)
            arrival = delivery["delivered_at"] if delivery and delivery["state"] == "acknowledged" else None
            admitted = admissions.get(row["message_id"])
            attempts = [{k: c.get(k) for k in ("time", "agent_id", "authorization_valid", "accepted", "reason")}
                        for c in tool_calls if c["operation"] == "tag"
                        and c.get("authorization_id") == grant["authorization_id"]]
            result.append({**grant, "message_id": row["message_id"], "uplink_enqueued_at": row["sent_at"],
                           "uplink_sent_at": row["uplink_sent_at"],
                           "server_received_at": admitted["server_received_at"] if admitted else None,
                           "delivered_at": arrival,
                           "expired_by_end": grant["expires_at"] <= end_time,
                           "expired_on_delivery": arrival is not None and arrival >= grant["expires_at"],
                           "uplink_wait_seconds": None if admitted is None else admitted["server_received_at"] - grant["issued_at"],
                           "downlink_wait_seconds": None if arrival is None else arrival - admitted["server_received_at"],
                           "attempts": attempts})
    return sorted(result, key=lambda row: (row["issued_at"], row["marker_id"]))
