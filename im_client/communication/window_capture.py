"""Agent-local window-mode executor. The controller never handles observations.

Only delivered group messages update peer knowledge. Local observe/tag triggers
are common to all three conditions; closed communication does not stop tools.
"""

import asyncio
from copy import deepcopy
import json
from math import hypot
from uuid import uuid4

from common.capture_protocol import TASK_PREFIX, ROUND_KEY, validate_contract, decode_receipt_report
from common.communication_windows import MESSAGE_PREFIX, ACK_PREFIX, is_closed, resolve_profile, validate_profile
from .capture_state import plan_waypoints
from simulator.experiment_rules import RULES, validate_authorization, authorization_valid

PHASES = ("begin", "uplink", "observe", "position", "plan", "move", "authorize", "tag", "finish")


class CaptureWindowSession:
    def __init__(self, name, comm_id, episode_id, members, period, llm, execute, send, profile=None):
        if not ROUND_KEY.fullmatch(episode_id + ":0") or type(period) is not int or period < 1:
            raise ValueError("invalid local window episode or period")
        self.name, self.comm_id, self.episode = name, comm_id, episode_id
        self.roles = {m["id"]: m["role"] for m in members}
        if name not in self.roles or list(self.roles.values()).count("leader") != 1:
            raise ValueError("local agent is not in the selected window team")
        self.role, self.period = self.roles[name], period
        self.profile = resolve_profile(members) if profile is None else validate_profile(profile)
        if {m["id"]: m["role"] for m in self.profile["members"]} != self.roles:
            raise ValueError("window profile differs from the local team")
        self.llm, self.execute, self.send = llm, execute, send
        self.time, self.phase = -1, "finish"
        self.sent_count, self.error, self.closed = 0, None, False
        self.generated_count, self.capture_known_at = 0, None
        self.outbox, self.uplink_messages = [], []
        self.lock = asyncio.Lock()
        self.observations, self.waypoints, self.waypoint_times = {}, {}, {}
        # Latest sample (including a negative one) and latest positive sighting
        # are separate: a late positive must not undo a more recent loss.
        self.observation_samples, self.observation_sources = {}, {}
        # Only this agent's own real sensor receipt controls autonomous follow.
        # Peer reports and queued leader guidance cannot impersonate local sight.
        self.local_observation = None
        self.desired, self.own_waypoint_report = {}, None
        self.observation_signature = None
        self.command, self.executed, self.events = None, [], []
        self.received, self.result_ids = set(), set()
        self.positions, self.grants, self.issued_grants = {}, {}, {}
        self.expired_grants = set()

    def event(self, event, **fields):
        self.events.append({"time": self.time, "agent_id": self.name, "event": event, **fields})

    def status(self):
        # Deliberately no target coordinates or peer observations in control responses.
        return {"agent_name": self.name, "comm_id": self.comm_id, "episode_id": self.episode,
                "time": self.time, "phase": self.phase, "busy": self.lock.locked(),
                "error": self.error, "closed": self.closed, "sent_count": self.sent_count,
                "generated_count": self.generated_count, "uplink_queued": len(self.outbox),
                "capture_known_at": self.capture_known_at,
                "executed": deepcopy(self.executed), "received_count": len(self.received)}

    async def publish(self, kind, payload):
        if self.closed:
            raise RuntimeError("cannot publish after closing the window session")
        self.generated_count += 1
        packet = {"message_id": uuid4().hex, "kind": kind, "payload": payload,
                  "sent_at": self.time, "sender_sequence": self.generated_count}
        content = MESSAGE_PREFIX + json.dumps(packet, ensure_ascii=False)
        round_id = f"{self.episode}:{self.time}"
        self.event("generate", message_id=packet["message_id"], sender_sequence=self.generated_count, kind=kind)
        # Own content is known locally without a network round trip. No other
        # agent's information is introduced by this local echo.
        await self.receive({"sender": self.name, "comm_id": self.comm_id, "proposal_id": round_id,
            "content": content, "window_delivery": {"message_id": packet["message_id"],
                "receiver": self.name, "sent_at": self.time, "delivered_at": self.time, "local_echo": True}},
            acknowledge=False)
        row = {"message_id": packet["message_id"], "sender_sequence": self.generated_count,
               "sent_at": self.time, "uplink_sent_at": None, "state": "queued",
               "proposal_id": round_id, "content": content}
        self.outbox.append(row)
        self.uplink_messages.append(row)
        self.event("uplink_enqueue", message_id=row["message_id"])
        await self.flush_uplink()

    async def flush_uplink(self):
        if self.closed or is_closed(self.profile, self.name, self.time):
            return
        while self.outbox:
            row = self.outbox[0]
            await self.send(row["content"], row["proposal_id"])
            row["state"], row["uplink_sent_at"] = "sent", self.time
            self.outbox.pop(0)
            self.sent_count += 1
            self.event("uplink_send", message_id=row["message_id"], sent_at=row["sent_at"],
                       uplink_sent_at=self.time, sender_sequence=row["sender_sequence"])

    def record_report(self, sender, contract, report, *, sent_at=None):
        validate_contract(contract, contract["round_id"], sender)
        episode, second = contract["round_id"].split(":")
        if episode != self.episode or int(second) > self.time:
            raise ValueError("future or foreign window tool result")
        allowed = {"observer": {"sim_observe", "sim_move_to"},
                   "leader": {"sim_move_to"}, "marker": {"sim_self_state", "sim_move_to", "sim_try_tag"}}
        if contract["tool"] not in allowed[self.roles[sender]]:
            raise ValueError("window result violates sender role")
        result = decode_receipt_report(report, contract)
        if contract["id"] in self.result_ids:
            return
        self.result_ids.add(contract["id"])
        if contract["tool"] == "sim_self_state":
            old = self.positions.get(sender)
            if old is None or result["time"] > old["position_at"]:
                self.positions[sender] = {"position": list(result["position"]), "position_at": result["time"],
                                          "sent_at": int(second) if sent_at is None else sent_at,
                                          "received_at": self.time}
            self.event("position_recorded", marker_id=sender, position_at=result["time"],
                       received_at=self.time)
        if contract["tool"] == "sim_observe" and sender == self.name:
            if (self.local_observation is None
                    or result["observed_at"] >= self.local_observation["observed_at"]):
                self.local_observation = deepcopy(result)
        if contract["tool"] == "sim_observe":
            source = {"sent_at": int(second) if sent_at is None else sent_at, "received_at": self.time}
            old_sample = self.observation_samples.get(sender)
            sample_updated = old_sample is None or result["observed_at"] > old_sample["result"]["observed_at"]
            if sample_updated:
                self.observation_samples[sender] = {"result": deepcopy(result), **source}
            old_sighting = self.observations.get(sender)
            sighting_updated = (result["target_position"] is not None and
                                (old_sighting is None or result["observed_at"] > old_sighting["observed_at"]))
            if sighting_updated:
                self.observations[sender] = deepcopy(result)
                self.observation_sources[sender] = source
            self.event("observation_recorded", observer_id=sender, observed_at=result["observed_at"],
                       sent_at=source["sent_at"], received_at=source["received_at"],
                       visible=result["target_position"] is not None, latest_sample_updated=sample_updated,
                       last_seen_updated=sighting_updated)
        if contract["tool"] == "sim_move_to" and int(second) >= self.waypoint_times.get(sender, -1):
            self.waypoints[sender] = list(result["waypoint"])
            self.waypoint_times[sender] = int(second)
        if (contract["tool"] == "sim_try_tag" and self.capture_known_at is None
                and (result["accepted"] or result.get("reason") == "already_captured")):
            self.capture_known_at = self.time
        if contract["tool"] == "sim_try_tag":
            self.event("tag_feedback", marker_id=sender, executed_at=int(second),
                       authorization_id=contract.get("authorization_id"),
                       accepted=result["accepted"], reason=result.get("reason"), received_at=self.time)

    async def receive(self, message, *, acknowledge=True):
        meta = message.get("window_delivery")
        if (not isinstance(meta, dict) or meta.get("receiver") != self.name
                or meta.get("delivered_at") != self.time or message.get("comm_id") != self.comm_id
                or message.get("sender") not in self.roles
                or message.get("proposal_id") != f"{self.episode}:{meta.get('sent_at')}"
                or type(meta.get("sent_at")) is not int or not 0 <= meta["sent_at"] <= self.time):
            raise ValueError("window delivery has wrong recipient, episode or time")
        content = message["content"]
        if not content.startswith(MESSAGE_PREFIX):
            raise ValueError("invalid window delivery envelope")
        packet = json.loads(content[len(MESSAGE_PREFIX):])
        if packet["message_id"] != meta["message_id"] or packet.get("sent_at") != meta["sent_at"]:
            raise ValueError("window delivery ID/time mismatch")
        if acknowledge and is_closed(self.profile, self.name, self.time):
            raise ValueError("received a downlink during the local closed window")
        if packet["message_id"] not in self.received:
            sender, payload = message["sender"], packet["payload"]
            if packet["kind"] in {"result", "position"}:
                if (packet["kind"] == "position") != (payload["contract"]["tool"] == "sim_self_state"):
                    raise ValueError("position messages require a real self-state contract receipt")
                if int(payload["contract"]["round_id"].split(":")[1]) > meta["sent_at"]:
                    raise ValueError("tool result claims execution after the message was sent")
                self.record_report(sender, payload["contract"], payload["report"], sent_at=meta["sent_at"])
            elif packet["kind"] == "authorization":
                grant = validate_authorization(payload, self.episode, self.roles)
                if grant["leader_id"] != sender or grant["issued_at"] != meta["sent_at"]:
                    raise ValueError("authorization sender/time mismatch")
                if grant["marker_id"] == self.name:
                    gid = grant["authorization_id"]
                    if gid in self.grants and self.grants[gid]["grant"] != grant:
                        raise ValueError("authorization ID reused")
                    self.grants[gid] = {"grant": deepcopy(grant), "received_at": self.time}
                    self.event("authorization_delivered", authorization_id=gid,
                               issued_at=grant["issued_at"], expires_at=grant["expires_at"],
                               message_id=packet["message_id"], usable=authorization_valid(grant, self.time))
                    self.expire_authorizations()
            elif packet["kind"] == "waypoint":
                if self.roles[sender] != "leader":
                    raise ValueError("only the leader can issue waypoint commands")
                names = set()
                for contract in payload:
                    name = contract["agent_id"]
                    validate_contract(contract, message["proposal_id"], name)
                    if name not in self.roles or name in names or contract["tool"] != "sim_move_to":
                        raise ValueError("invalid window waypoint batch")
                    names.add(name)
                    if name != self.name:
                        continue
                    if meta["sent_at"] != self.time or PHASES.index(self.phase) >= PHASES.index("move"):
                        self.event("expired_command", message_id=packet["message_id"], sent_at=meta["sent_at"])
                    else:
                        if self.command is not None and self.command != contract:
                            raise ValueError("conflicting current-tick waypoint commands")
                        self.command = deepcopy(contract)
            else:
                raise ValueError("unsupported window packet kind")
            self.received.add(packet["message_id"])
            self.event("receive", message_id=packet["message_id"], sent_at=meta["sent_at"],
                       server_received_at=meta.get("server_received_at"),
                       delivered_at=meta["delivered_at"], local_echo=not acknowledge, kind=packet["kind"])
        if acknowledge:
            await self.send(ACK_PREFIX + json.dumps({"message_id": packet["message_id"]}),
                            f"{self.episode}:{self.time}")

    def contract(self, tool, arguments=None, **extra):
        return {"id": uuid4().hex, "round_id": f"{self.episode}:{self.time}",
                "agent_id": self.name, "tool": tool, "arguments": arguments or {}, **extra}

    async def act(self, contract):
        validate_contract(contract, f"{self.episode}:{self.time}", self.name)
        self.event("tool_start", contract_id=contract["id"], tool=contract["tool"])
        report = await self.execute(TASK_PREFIX + json.dumps(contract, ensure_ascii=False),
                                    sim_round_id=contract["round_id"])
        if self.closed:
            raise RuntimeError("window session closed while local tool was running")
        self.record_report(self.name, contract, report)
        self.executed.append({"contract_id": contract["id"], "tool": contract["tool"]})
        self.event("tool_complete", contract_id=contract["id"], tool=contract["tool"])
        payload = {"contract": contract, "report": report}
        if contract["tool"] == "sim_move_to":
            self.own_waypoint_report = deepcopy(payload)
        await self.publish("position" if contract["tool"] == "sim_self_state" else "result", payload)

    def expire_authorizations(self):
        rows = [row["grant"] for row in self.grants.values()] + list(self.issued_grants.values())
        for grant in rows:
            gid = grant["authorization_id"]
            if self.time >= grant["expires_at"] and gid not in self.expired_grants:
                self.expired_grants.add(gid)
                self.event("authorization_expired", authorization_id=gid, expires_at=grant["expires_at"])

    async def authorize(self):
        if self.role != "leader":
            return
        # Use the latest SAMPLE per observer, never its historical positive after
        # a more recent negative. A different observer may still provide evidence.
        samples = [row["result"] for row in self.observation_samples.values()
                   if row["result"]["target_position"] is not None
                   and 0 <= self.time - row["result"]["observed_at"] <= RULES["information_max_age"]]
        sample = max(samples, key=lambda s: (s["observed_at"], s["observer_id"]), default=None)
        for marker in sorted(n for n, role in self.roles.items() if role == "marker"):
            previous = self.issued_grants.get(marker)
            position = self.positions.get(marker)
            reason, estimate = None, None
            if previous and authorization_valid(previous, self.time):
                reason = "previous_authorization_unexpired"
            elif sample is None:
                reason = "no_fresh_positive_sample"
            elif position is None or not 0 <= self.time - position["position_at"] <= RULES["information_max_age"]:
                reason = "no_fresh_marker_position"
            else:
                estimate = hypot(*(a-b for a, b in zip(sample["target_position"], position["position"])))
                if estimate > RULES["authorization_distance"] + 1e-9:
                    reason = "reported_distance_outside_radius"
            self.event("authorization_decision", marker_id=marker, reason=reason or "issued",
                       observed_at=sample["observed_at"] if sample else None,
                       position_at=position["position_at"] if position else None, estimated_distance=estimate)
            if reason:
                continue
            grant = {"authorization_id": uuid4().hex, "episode_id": self.episode,
                     "leader_id": self.name, "marker_id": marker, "observer_id": sample["observer_id"],
                     "observed_at": sample["observed_at"], "target_position": list(sample["target_position"]),
                     "position_at": position["position_at"], "marker_position": list(position["position"]),
                     "issued_at": self.time, "expires_at": self.time + RULES["authorization_ttl"],
                     "estimated_distance": estimate, "rules_version": RULES["version"]}
            validate_authorization(grant, self.episode, self.roles)
            self.issued_grants[marker] = deepcopy(grant)
            self.event("authorization_issued", **deepcopy(grant))
            await self.publish("authorization", grant)

    async def tag(self):
        if self.role != "marker":
            return
        usable = [row for row in self.grants.values() if authorization_valid(row["grant"], self.time)]
        if not usable:
            self.event("tag_skipped", reason="no_delivered_valid_authorization")
            return
        row = max(usable, key=lambda row: row["grant"]["issued_at"])
        grant = row["grant"]
        self.event("authorized_tag_attempt", authorization_id=grant["authorization_id"],
                   received_at=row["received_at"], expires_at=grant["expires_at"])
        await self.act(self.contract("sim_try_tag", authorization_id=grant["authorization_id"]))

    async def plan(self):
        if self.role != "leader":
            return
        sample_time = self.time - self.time % self.period
        observer_status = {}
        for name, role in self.roles.items():
            if role != "observer":
                continue
            latest = self.observation_samples.get(name)
            sample = latest["result"] if latest else None
            sighting = self.observations.get(name)
            source = self.observation_sources.get(name)
            observer_status[name] = {
                "latest_sample": deepcopy(sample),
                "latest_sample_sent_at": latest["sent_at"] if latest else None,
                "latest_sample_received_at": latest["received_at"] if latest else None,
                "latest_sample_age": self.time - sample["observed_at"] if sample else None,
                "visible_at_latest_sample": sample["target_position"] is not None if sample else None,
                "reported_current_sample": bool(sample and sample["observed_at"] == sample_time),
                "last_seen": deepcopy(sighting),
                "last_seen_sent_at": source["sent_at"] if source else None,
                "last_seen_received_at": source["received_at"] if source else None,
                "last_seen_age": self.time - sighting["observed_at"] if sighting else None,
            }
        recipients = [name for name, role in self.roles.items()
                      if role != "observer" or not (observer_status[name]["reported_current_sample"]
                                                     and observer_status[name]["visible_at_latest_sample"])]
        # Do not call the model again just because an age counter advanced.
        # New negative samples and new last sightings do trigger replanning.
        signature = json.dumps([self.observation_samples, self.observations], sort_keys=True)
        if signature != self.observation_signature:
            if not self.observations:
                if self.time == 0:
                    raise ValueError("initial leader has no delivered observation")
            else:
                state = {"time": self.time, "roles": self.roles,
                         "observations": deepcopy(self.observations), "waypoints": deepcopy(self.waypoints),
                         "observer_status": observer_status, "waypoint_recipients": recipients}
                actions = await plan_waypoints(self.llm, state)
                # A fresh plan supersedes pending destinations, including an
                # explicit return to an already accepted point filtered by
                # the shared planner. Do not resurrect an obsolete command.
                self.desired = {action["agent_id"]: action for action in actions}
            self.observation_signature = signature
        commands = []
        for name, action in self.desired.items():
            args = action["arguments"]
            if self.waypoints.get(name) != [args["x"], args["y"]]:
                commands.append({**deepcopy(action), "id": uuid4().hex,
                                 "round_id": f"{self.episode}:{self.time}"})
        if commands:
            # Reissue unacknowledged destinations in the CURRENT tick. Old
            # queued commands remain historical and are never silently rebound.
            await self.publish("waypoint", commands)

    async def move(self):
        command = self.command
        sample = self.local_observation
        sample_time = self.time - self.time % self.period
        if (self.role == "observer" and sample is not None
                and sample["target_position"] is not None and sample["observed_at"] == sample_time):
            # Local sensing/movement continues through a closed window. Keep
            # observed_at unchanged between samples; never query target truth.
            if command is not None:
                self.event("observer_guidance_ignored", contract_id=command["id"],
                           reason="local_target_visible", observed_at=sample["observed_at"])
            point = sample["target_position"]
            if self.waypoints.get(self.name) != point:
                self.event("observer_follow", observed_at=sample["observed_at"], waypoint=list(point))
                await self.act(self.contract("sim_move_to", {"x": point[0], "y": point[1]},
                                             observed_at=sample["observed_at"]))
            return
        if command is not None:
            args = command["arguments"]
            if self.waypoints.get(self.name) == [args["x"], args["y"]]:
                # Re-send the OLD real receipt, not a fictitious new action.
                if self.own_waypoint_report is None:
                    raise ValueError("known local waypoint has no executor evidence")
                await self.publish("result", self.own_waypoint_report)
            else:
                await self.act(command)

    async def tick(self, second, phase):
        async with self.lock:
            if self.closed or self.error:
                raise RuntimeError(self.error or "window session closed")
            try:
                if type(second) is not int or phase not in PHASES:
                    raise ValueError("invalid local window tick")
                if phase == self.phase and second == self.time:
                    # This response describes the completed request, not the
                    # lock held briefly while checking its idempotency.
                    return {**self.status(), "busy": False}
                if phase == "begin":
                    if second != self.time + 1 or self.phase != "finish":
                        raise ValueError("previous local tick has not finished")
                    self.time, self.command, self.executed = second, None, []
                    self.expire_authorizations()
                elif second != self.time or PHASES.index(phase) != PHASES.index(self.phase) + 1:
                    raise ValueError("local window phases must run in order")
                if phase == "uplink":
                    await self.flush_uplink()
                elif phase == "observe" and self.role == "observer" and second % self.period == 0:
                    await self.act(self.contract("sim_observe"))
                elif phase == "plan":
                    await self.plan()
                elif phase == "position" and self.role == "marker":
                    await self.act(self.contract("sim_self_state"))
                elif phase == "move":
                    await self.move()
                elif phase == "authorize":
                    await self.authorize()
                elif phase == "tag":
                    await self.tag()
                self.phase = phase
                self.event("phase_complete", phase=phase)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                raise
        return self.status()
