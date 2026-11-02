"""Synchronous 5-open/5-closed windows and server-side downlink queues.

Closed uplinks stay at the client. Once admitted to the server, a message only
depends on its receiver's availability. All times are simulation seconds.
"""

from copy import deepcopy
import json
from itertools import combinations
from common.capture_protocol import ROUND_KEY
from simulator.experiment_rules import validate_authorization

PROTOCOL_VERSION = 3
MESSAGE_PREFIX = "IOA_WINDOW_MESSAGE_V3\n"
ACK_PREFIX = "IOA_WINDOW_ACK_V3\n"
CONDITIONS = ("continuous", "nonleader_pair", "leader_pair")
SCHEDULE = {"type": "periodic", "open_seconds": 5, "closed_seconds": 5,
            "period_seconds": 10, "phase_seconds": 0}


def pair_catalog(members):
    """Stable numbering: observer, leader, marker; ID order within each role."""
    roles = {m["id"]: m["role"] for m in members}
    if (len(roles) != len(members) or len(roles) != 5
            or any(not isinstance(n, str) or not n for n in roles)
            or list(roles.values()).count("leader") != 1
            or list(roles.values()).count("observer") != 2
            or list(roles.values()).count("marker") != 2):
        raise ValueError("window pilot requires the selected 2/1/2 five-member team")
    order = {"observer": 0, "leader": 1, "marker": 2}
    names = sorted(roles, key=lambda n: (order[roles[n]], n))
    return [{"pair_id": i, "restricted": list(pair),
             "condition": "leader_pair" if any(roles[n] == "leader" for n in pair) else "nonleader_pair"}
            for i, pair in enumerate(combinations(names, 2), 1)]


def resolve_profile(members, condition=None, *, pair_id=None, restricted=None, schedule=None):
    catalog = pair_catalog(members)
    if condition is not None and condition not in CONDITIONS:
        raise ValueError("invalid communication condition")
    # This pilot deliberately has one shared schedule and no per-agent phases.
    if schedule is not None and (schedule != SCHEDULE or any(
            type(schedule[k]) is not int for k in SCHEDULE if k != "type")):
        raise ValueError("schedule must be synchronous: open 5s, closed 5s, period 10s, phase 0")
    if pair_id is not None and (type(pair_id) is not int or not 1 <= pair_id <= 10):
        raise ValueError("pair_id must be an integer from 1 to 10; use --list-pairs")
    if restricted is not None:
        if (not isinstance(restricted, list) or any(not isinstance(n, str) for n in restricted)
                or len(set(restricted)) != len(restricted)):
            raise ValueError("invalid restricted member list")
        match = next((row for row in catalog if set(row["restricted"]) == set(restricted)), None)
        if restricted and match is None:
            raise ValueError("restricted must select exactly two members of the selected team")
        selected_id = match["pair_id"] if match else None
        if pair_id is not None and pair_id != selected_id:
            raise ValueError("pair_id and restricted member list disagree")
        pair_id = selected_id
    selected = catalog[pair_id - 1] if pair_id is not None else {
        "pair_id": None, "restricted": [], "condition": "continuous"}
    if condition is not None and condition != selected["condition"]:
        raise ValueError("condition disagrees with the selected pair; specify --pair 1..10 for a restricted run")
    return {"version": PROTOCOL_VERSION, **deepcopy(selected), "selection": "explicit_pair",
            "members": deepcopy(members), "schedule": deepcopy(SCHEDULE), "time_unit": "simulation_seconds"}


def profile_from_config(members, config):
    unknown = set(config) - {"condition", "pair_id", "restricted", "schedule"}
    if unknown:
        raise ValueError(f"unsupported window settings {sorted(unknown)}; use pair_id and periodic schedule (no random seed/closed_windows)")
    return resolve_profile(members, **config)


def validate_profile(profile):
    if profile.get("version") != PROTOCOL_VERSION:
        raise ValueError("window protocol version mismatch; rebuild server and clients")
    return resolve_profile(profile["members"], profile["condition"], pair_id=profile["pair_id"],
                           restricted=profile["restricted"], schedule=profile["schedule"])


def is_closed(profile, member, second):
    if type(second) is not int or second < 0:
        raise ValueError("window time must be a nonnegative simulation second")
    schedule = profile["schedule"]
    return (member in profile["restricted"]
            and second % schedule["period_seconds"] >= schedule["open_seconds"])


class WindowRouter:
    def __init__(self, episode_id, comm_id, profile):
        if not isinstance(episode_id, str) or not ROUND_KEY.fullmatch(episode_id + ":0"):
            raise ValueError("invalid window episode ID")
        self.episode_id, self.comm_id = episode_id, comm_id
        self.profile = validate_profile(profile)
        self.names = [m["id"] for m in profile["members"]]
        self.time, self.sealed, self.closed = 0, False, False
        self.messages, self.events, self.deliveries = [], [], []
        self.ids = {}
        self.queues = {name: [] for name in self.names}
        self.seen = {name: 0 for name in self.names}
        self.last_generated_at = {name: -1 for name in self.names}

    def event(self, event, **fields):
        self.events.append({"event": event, "time": self.time, **fields})

    def accept(self, message, sender):
        if self.closed or self.sealed:
            raise ValueError("window session is closed/sealed")
        if (sender not in self.names or message.get("sender") != sender
                or message.get("comm_id") != self.comm_id):
            raise ValueError("window message has wrong sender, episode or simulation time")
        content = message.get("content", "")
        if not content.startswith(MESSAGE_PREFIX):
            raise ValueError("window session requires the window message protocol")
        packet = json.loads(content[len(MESSAGE_PREFIX):])
        mid = packet.get("message_id")
        if not isinstance(mid, str) or not mid or packet.get("kind") not in {"result", "waypoint", "position", "authorization"}:
            raise ValueError("invalid window message")
        if mid in self.ids:
            if self.messages[self.ids[mid]]["message"] != message:
                raise ValueError("message ID reused with different content")
            return False
        generated = packet.get("sent_at")
        if (type(generated) is not int or not self.last_generated_at[sender] <= generated <= self.time
                or generated < 0 or message.get("proposal_id") != f"{self.episode_id}:{generated}"):
            raise ValueError("window message has invalid original generation time")
        if type(packet.get("sender_sequence")) is not int or packet["sender_sequence"] != self.seen[sender] + 1:
            raise ValueError("uplink must preserve sender sequence without gaps or duplicates")
        if is_closed(self.profile, sender, self.time):
            raise ValueError("closed uplink must remain in the client's outbox")
        if packet["kind"] == "authorization":
            grant = validate_authorization(packet["payload"], self.episode_id,
                                           {m["id"]: m["role"] for m in self.profile["members"]})
            if grant["leader_id"] != sender or grant["issued_at"] != generated:
                raise ValueError("only the leader may issue an authorization at its original time")
        seq = len(self.messages) + 1
        self.ids[mid] = seq - 1
        row = {"message_id": mid, "sequence": seq, "sender_sequence": packet["sender_sequence"],
               "sent_at": generated, "server_received_at": self.time, "message": deepcopy(message)}
        self.messages.append(row)
        self.seen[sender] += 1
        self.last_generated_at[sender] = generated
        self.event("server_receive", message_id=mid, sequence=seq, sender=sender,
                   sent_at=generated, server_received_at=self.time, sender_sequence=packet["sender_sequence"])
        for receiver in self.names:
            if receiver == sender:
                continue  # Local processing already happened without network access.
            delivery = {"message_id": mid, "sequence": seq, "sender": sender, "receiver": receiver,
                        "sender_sequence": packet["sender_sequence"], "sent_at": generated,
                        "server_received_at": self.time, "delivered_at": None, "state": "queued", "local_echo": False}
            self.deliveries.append(delivery)
            self.queues[receiver].append(delivery)
            self.event("downlink_enqueue", message_id=mid, receiver=receiver, kind=packet["kind"])
        return True

    def authorization_receipts(self):
        """Controller-only evidence of actual marker delivery; never a command."""
        receipts = []
        for row in self.deliveries:
            if row["state"] != "acknowledged":
                continue
            packet = json.loads(self.messages[row["sequence"] - 1]["message"]["content"][len(MESSAGE_PREFIX):])
            if packet["kind"] == "authorization" and row["receiver"] == packet["payload"]["marker_id"]:
                receipts.append({"grant": deepcopy(packet["payload"]), "message_id": row["message_id"],
                                 "delivered_at": row["delivered_at"]})
        return receipts

    def eligible(self, row):
        return not is_closed(self.profile, row["receiver"], self.time)

    def ready(self):
        if self.closed:
            return []
        ready = []
        for queue in self.queues.values():
            if any(row["state"] == "inflight" for row in queue):
                continue
            row = next((row for row in queue if self.eligible(row)), None)
            if row is not None:
                ready.append(row)
        return sorted(ready, key=lambda row: row["sequence"])

    def envelope(self, row):
        message = deepcopy(self.messages[row["sequence"] - 1]["message"])
        message["window_delivery"] = {k: row[k] for k in
            ("message_id", "sequence", "sender_sequence", "sender", "receiver", "sent_at", "server_received_at", "local_echo")}
        message["window_delivery"]["delivered_at"] = self.time
        return message

    def sent(self, row):
        if row["state"] != "queued":
            raise ValueError("duplicate transport send")
        row["state"], row["delivered_at"] = "inflight", self.time
        self.event("deliver", **{k: row[k] for k in
                   ("message_id", "sequence", "sender", "receiver", "sent_at", "server_received_at", "delivered_at", "local_echo")})

    def acknowledge(self, receiver, message_id):
        queue = self.queues.get(receiver, [])
        row = next((row for row in queue if row["message_id"] == message_id
                    and row["state"] == "inflight"), None)
        if row is not None:
            queue.remove(row)
            row["state"] = "acknowledged"
            self.event("acknowledge", message_id=message_id, receiver=receiver)
            return
        if any(r["receiver"] == receiver and r["message_id"] == message_id
               and r["state"] == "acknowledged" for r in self.deliveries):
            return  # Duplicate ACK does not duplicate a delivery.
        raise ValueError("unexpected delivery acknowledgement")

    def status(self, expected=None):
        if expected is not None and (set(expected) != set(self.names)
                or any(type(n) is not int or n < 0 for n in expected.values())):
            raise ValueError("expected sends must cover every selected member")
        seen = expected is None or all(self.seen[n] == expected[n] for n in self.names)
        inflight = sum(r["state"] == "inflight" for r in self.deliveries)
        return {"time": self.time, "seen_by_sender": dict(self.seen),
                "settled": seen and not inflight and not self.ready(),
                "inflight": inflight, "queued": sum(r["state"] == "queued" for r in self.deliveries),
                "sealed": self.sealed, "closed": self.closed}

    def begin(self, second):
        if type(second) is not int or self.closed:
            raise ValueError("invalid window clock")
        if second == self.time and not self.sealed:
            return
        if second != self.time + 1 or not self.sealed:
            raise ValueError("window clock must advance once after sealing the previous tick")
        self.time, self.sealed = second, False
        self.event("tick", closed_members=[n for n in self.names if is_closed(self.profile, n, second)])

    def seal(self, expected):
        if not self.status(expected)["settled"]:
            raise ValueError("local sends or eligible deliveries have not finished")
        self.sealed = True
        self.event("seal", queued=self.status()["queued"])

    def close(self):
        self.closed = True
        self.event("close", queued=self.status()["queued"])

    def export(self):
        return deepcopy({"episode_id": self.episode_id, "comm_id": self.comm_id, "profile": self.profile,
                         "time": self.time, "messages": self.messages, "deliveries": self.deliveries,
                         "events": self.events, "closed": self.closed})
