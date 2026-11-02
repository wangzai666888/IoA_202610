"""Window transport and full episodes using real code/tools, scripted model only.

FastAPI endpoint functions are AST-loaded to avoid Docker/Milvus dependencies.
Only model answers, persistence and socket transport are substituted. Simulator
tools still cross the real HTTP boundary and production receipt/audit checks.
"""
import ast
import asyncio
from collections import defaultdict
from copy import deepcopy
import json
import io
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import secrets
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

from common.capture_protocol import TASK_PREFIX, decode_task
from common.communication_windows import (
    ACK_PREFIX, MESSAGE_PREFIX, PROTOCOL_VERSION, SCHEDULE, WindowRouter,
    is_closed, resolve_profile, pair_catalog, profile_from_config,
)
from simulator.http_client import AgentClient, ControllerClient
from simulator.ioa_window_run import run_window_episode, cli_config, check_window_protocol, main
from simulator.service import SimulationHTTPServer, SimulationService
from simulator.fixed_scenarios import fixed_scene
from simulator.tests.capture_helpers import load_helper
from simulator.tests.test_capture_evidence import Model, call, guard, make_agent
from simulator.tests.test_capture_performance import Message, load_runtime
from simulator.tests.test_capture_phases import CopyBank, EPISODE, MEMBERS, NAMES, ROLES, ROOT, receipt

CaptureWindowSession = load_helper("window_capture").CaptureWindowSession


class WireMessage(Message):
    def model_dump(self, **kwargs): return deepcopy(vars(self))
    @classmethod
    def model_validate(cls, data): return cls(**deepcopy(data))


def packet(sender, second, mid, kind="result", payload=None, sender_sequence=1):
    return {"sender": sender, "comm_id": "comm", "proposal_id": f"{EPISODE}:{second}",
            "content": MESSAGE_PREFIX + json.dumps({"message_id": mid, "kind": kind, "payload": payload,
                                                   "sent_at": second, "sender_sequence": sender_sequence})}


def flush(router):
    delivered = []
    while router.ready():
        for row in router.ready():
            delivered.append(router.envelope(row))
            router.sent(row)
            router.acknowledge(row["receiver"], row["message_id"])
    return delivered


def advance(router, second):
    while router.time < second:
        router.seal(dict(router.seen))
        router.begin(router.time + 1)


class WindowTransportTests(unittest.TestCase):
    def test_all_ten_pairs_are_stable_and_cover_six_nonleader_four_leader_cases(self):
        catalog = pair_catalog(MEMBERS)
        self.assertEqual(catalog, pair_catalog(list(reversed(MEMBERS))))
        self.assertEqual([r["pair_id"] for r in catalog], list(range(1, 11)))
        self.assertEqual(len({frozenset(r["restricted"]) for r in catalog}), 10)
        self.assertEqual(sum(r["condition"] == "nonleader_pair" for r in catalog), 6)
        self.assertEqual(sum(r["condition"] == "leader_pair" for r in catalog), 4)
        self.assertEqual(catalog[0]["restricted"], NAMES[:2])
        self.assertEqual(catalog[1]["restricted"], [NAMES[0], NAMES[2]])
        self.assertEqual(set(catalog[-1]["restricted"]), set(NAMES[3:]))
        for row in catalog:
            profile = resolve_profile(MEMBERS, pair_id=row["pair_id"])
            self.assertEqual(profile["restricted"], row["restricted"])
            self.assertEqual(profile["condition"], row["condition"])
        for pair in (0, 11, True, 1.0, "1"):
            with self.assertRaises(ValueError): resolve_profile(MEMBERS, pair_id=pair)
        with self.assertRaises(ValueError): resolve_profile(MEMBERS, "leader_pair")
        with self.assertRaises(ValueError): resolve_profile(MEMBERS, "continuous", pair_id=1)
        with self.assertRaises(ValueError): resolve_profile(MEMBERS, pair_id=1, restricted=NAMES[3:])

    def test_synchronous_periodic_boundaries_through_120_and_no_phase_or_legacy_settings(self):
        for pair in range(1, 11):
            profile = resolve_profile(MEMBERS, pair_id=pair)
            for second in range(121):
                for name in NAMES:
                    self.assertEqual(is_closed(profile, name, second),
                                     name in profile["restricted"] and 5 <= second % 10 < 10)
        self.assertFalse(any(is_closed(resolve_profile(MEMBERS), n, t) for n in NAMES for t in range(121)))
        for changes in ({"phase_seconds": 5}, {"open_seconds": 10}, {"period_seconds": 30}, {"phase_seconds": False}):
            with self.assertRaises(ValueError): resolve_profile(MEMBERS, schedule=SCHEDULE | changes)
        for legacy in ({"seed": 0}, {"closed_windows": [[5, 8]]}):
            with self.assertRaises(ValueError): profile_from_config(MEMBERS, legacy)

    def test_cli_pair_overrides_default_continuous_and_list_requires_no_services(self):
        config = cli_config({"condition": "continuous", "schedule": SCHEDULE}, pair_id=2)
        self.assertEqual(profile_from_config(MEMBERS, config)["condition"], "leader_pair")
        baseline = cli_config(config, condition="continuous")
        self.assertEqual(profile_from_config(MEMBERS, baseline)["restricted"], [])
        out = io.StringIO()
        with patch("simulator.ioa_window_run.urlopen", side_effect=AssertionError("network used")), redirect_stdout(out):
            main(["--clients-file", str(ROOT / "simulator/five_clients.local.json"), "--list-pairs"])
        self.assertEqual(len(out.getvalue().splitlines()), 10)
        with patch("simulator.ioa_window_run.urlopen", side_effect=AssertionError("network used")), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--clients-file", str(ROOT / "simulator/five_clients.local.json"),
                      "--window-config", str(ROOT / "simulator/scenarios/communication_windows.json"),
                      "--pair", "1", "--condition", "continuous"])
            self.assertEqual(raised.exception.code, 2)
        with patch("simulator.ioa_window_run.urlopen", return_value=io.StringIO('{"protocol_version":1}')):
            with self.assertRaisesRegex(RuntimeError, "expected 3"): check_window_protocol("http://old-client")

    def test_downlink_fifo_open_links_no_duplicates_and_original_time(self):
        profile = resolve_profile(MEMBERS, "nonleader_pair", restricted=[NAMES[0], NAMES[3]])
        router = WindowRouter(EPISODE, "comm", profile)
        advance(router, 5)
        with self.assertRaisesRegex(ValueError, "closed uplink"):
            router.accept(packet(NAMES[0], 5, "blocked"), NAMES[0])
        messages = [packet(NAMES[1], 5, "a"), packet(NAMES[1], 5, "b", sender_sequence=2)]
        for message in messages: router.accept(message, message["sender"])
        self.assertFalse(router.accept(messages[0], NAMES[1]))
        first = flush(router)
        # Unrestricted peers continue receiving while two downlinks are closed.
        for message in first:
            meta = message["window_delivery"]
            self.assertNotIn(meta["receiver"], profile["restricted"])
        self.assertTrue(any(m["sender"] == NAMES[1] and m["window_delivery"]["receiver"] == NAMES[2]
                            for m in first))
        self.assertEqual([m["window_delivery"]["message_id"] for m in first
                          if m["window_delivery"]["receiver"] == NAMES[0]], [])
        self.assertTrue(router.status(dict(router.seen))["settled"])
        self.assertGreater(router.status()["queued"], 0)
        advance(router, 10)
        later = flush(router)
        for message in later:
            self.assertEqual(message["proposal_id"], EPISODE + ":5")
            self.assertEqual(message["window_delivery"]["sent_at"], 5)
            self.assertEqual(message["window_delivery"]["server_received_at"], 5)
            self.assertEqual(message["window_delivery"]["delivered_at"], 10)
        self.assertEqual(len(first + later), 8)
        pairs = [(m["window_delivery"]["message_id"], m["window_delivery"]["receiver"]) for m in first + later]
        self.assertEqual(len(set(pairs)), 8)
        for receiver in set(NAMES) - {NAMES[1]}:
            self.assertEqual([m["window_delivery"]["message_id"] for m in first + later
                              if m["sender"] == NAMES[1] and m["window_delivery"]["receiver"] == receiver], ["a", "b"])
        router.acknowledge(NAMES[2], "a")
        self.assertEqual(flush(router), [])

    def test_admitted_message_does_not_recheck_original_senders_window(self):
        router = WindowRouter(EPISODE, "comm", resolve_profile(MEMBERS, pair_id=1))
        advance(router, 4)
        router.accept(packet(NAMES[0], 4, "before-close"), NAMES[0])
        # Inspect availability at a later boundary independently of the normal
        # driver (which also waits for acknowledgements before advancing).
        router.time = 5
        self.assertTrue(is_closed(router.profile, NAMES[0], 5))
        self.assertTrue(any(r["receiver"] == NAMES[2] for r in router.ready()))
        self.assertFalse(any(r["receiver"] == NAMES[1] for r in router.ready()))

    def test_closed_uplink_stays_local_then_fifo_release_preserves_identity_and_old_time(self):
        async def exercise():
            profile = resolve_profile(MEMBERS, pair_id=10)
            router = WindowRouter(EPISODE, "comm", profile)
            advance(router, 5)
            async def send(content, round_id):
                router.accept({"sender": NAMES[3], "comm_id": "comm", "proposal_id": round_id,
                               "content": content}, NAMES[3])
            async def execute(text, sim_round_id):
                return receipt(decode_task(text, sim_round_id, NAMES[3]))
            session = CaptureWindowSession(NAMES[3], "comm", EPISODE, MEMBERS, 5, None, execute, send, profile)
            for second in (5, 8):
                advance(router, second)
                session.time, session.phase = second, "observe"
                await session.tick(second, "position")
            self.assertEqual(len(session.received), 2)  # Own results processed locally.
            self.assertEqual(session.sent_count, 0)
            self.assertEqual(router.messages, [])       # Nothing entered server/chat history.
            self.assertEqual(len(session.outbox), 2)
            old_ids = [r["message_id"] for r in session.outbox]
            advance(router, 10)
            session.time, session.phase = 10, "begin"
            await session.tick(10, "uplink")
            self.assertEqual([r["message_id"] for r in router.messages], old_ids)
            self.assertEqual([r["sent_at"] for r in router.messages], [5, 8])
            self.assertEqual([r["server_received_at"] for r in router.messages], [10, 10])
            self.assertEqual([r["sender_sequence"] for r in router.messages], [1, 2])
            self.assertEqual(len(flush(router)), 8)
            await session.tick(10, "uplink")
            self.assertEqual(session.sent_count, 2)
            self.assertEqual(session.outbox, [])
            # A second cycle closes again; ending the episode retains unsent data.
            advance(router, 15)
            session.time, session.phase = 15, "observe"
            await session.tick(15, "position")
            session.closed = True
            session.time = 40
            await session.flush_uplink()
            self.assertEqual(len(session.outbox), 1)
            self.assertEqual(session.outbox[0]["state"], "queued")
            self.assertEqual(len(router.messages), 2)
        asyncio.run(exercise())

    def test_pending_ack_or_uningested_send_blocks_clock_but_closed_backlog_does_not(self):
        router = WindowRouter(EPISODE, "comm", resolve_profile(MEMBERS))
        message = packet(NAMES[0], 0, "a")
        router.accept(message, NAMES[0])
        for row in router.ready(): router.sent(row)
        self.assertFalse(router.status(dict(router.seen))["settled"])
        with self.assertRaises(ValueError): router.seal(dict(router.seen))
        with self.assertRaises(ValueError): router.begin(1)
        for name in set(NAMES) - {NAMES[0]}: router.acknowledge(name, "a")
        expected = dict(router.seen)
        expected[NAMES[1]] = 1
        with self.assertRaises(ValueError): router.seal(expected)
        router.seal(dict(router.seen))
        with self.assertRaises(ValueError): router.accept(message, NAMES[0])
        router.begin(1)
        router.close()
        self.assertEqual(router.ready(), [])
        with self.assertRaises(ValueError): router.begin(2)

    def test_delayed_observation_remains_old_and_old_command_cannot_execute(self):
        async def exercise():
            sent, calls = [], []
            async def send(content, key): sent.append((content, key))
            async def execute(*args, **kwargs): calls.append(args); raise AssertionError("stale action executed")
            session = CaptureWindowSession(NAMES[3], "comm", EPISODE, MEMBERS, 5, None, execute, send)
            session.time, session.phase = 20, "uplink"
            observed = {"id": "observation", "round_id": EPISODE + ":10", "agent_id": NAMES[0],
                        "tool": "sim_observe", "arguments": {}}
            command = {"id": "old-command", "round_id": EPISODE + ":10", "agent_id": NAMES[3],
                       "tool": "sim_move_to", "arguments": {"x": 0, "y": 40}, "observed_at": 10}
            messages = [packet(NAMES[0], 10, "observation", payload={"contract": observed, "report": receipt(observed)}),
                        packet(NAMES[2], 10, "command", "waypoint", [command])]
            for message in messages:
                message["window_delivery"] = {"receiver": NAMES[3], "sent_at": 10, "delivered_at": 20,
                    "message_id": json.loads(message["content"][len(MESSAGE_PREFIX):])["message_id"]}
                await session.receive(message)
                await session.receive(message)  # Duplicate socket data is ACKed, not applied twice.
            self.assertEqual(session.observations[NAMES[0]]["observed_at"], 10)
            self.assertEqual(session.observations[NAMES[0]]["time"], 10)
            self.assertIsNone(session.command)
            session.phase = "position"
            for phase in ("plan", "move"):
                await session.tick(20, phase)
            self.assertEqual(calls, [])
            self.assertEqual(len(session.received), 2)
            self.assertEqual(len([e for e in session.events if e["event"] == "expired_command"]), 1)
            self.assertFalse((await session.tick(20, "move"))["busy"])
            self.assertNotIn("observations", session.status())
        asyncio.run(exercise())

    def test_new_plan_cancels_pending_destination_and_retries_do_not_repeat_actions(self):
        async def exercise():
            sent, executed = [], []
            async def send(content, key): sent.append(content)
            async def execute(text, sim_round_id):
                contract = decode_task(text, sim_round_id, NAMES[3])
                executed.append(contract)
                return receipt(contract)
            marker = CaptureWindowSession(NAMES[3], "comm", EPISODE, MEMBERS, 5, None, execute, send)
            for phase in ("begin", "uplink", "observe", "position", "plan", "move", "authorize", "tag", "finish"):
                await marker.tick(0, phase)
                await marker.tick(0, phase)
            self.assertEqual(len(executed), 1)
            leader = CaptureWindowSession(NAMES[2], "comm", EPISODE, MEMBERS, 5,
                Model([Message(content={"waypoints": [
                    {"agent_id": n, "x": 0, "y": 40, "observed_at": 10} for n in NAMES[1:]]})]),
                execute, send)
            leader.time, leader.phase = 10, "position"
            observed = {"id": "seen-10", "round_id": EPISODE + ":10", "agent_id": NAMES[0],
                        "tool": "sim_observe", "arguments": {}}
            leader.record_report(NAMES[0], observed, receipt(observed))
            leader.waypoints = {n: [0, 40] for n in NAMES[1:]}
            leader.desired[NAMES[3]] = {"agent_id": NAMES[3], "tool": "sim_move_to",
                                       "arguments": {"x": 5, "y": 40}, "observed_at": 5}
            before = len(sent)
            await leader.tick(10, "plan")
            self.assertEqual(leader.desired, {})
            self.assertEqual(len(sent), before)
        asyncio.run(exercise())


class ObserverFollowTests(unittest.TestCase):
    def test_local_follow_updates_while_closed_without_leader_or_peer_truth(self):
        async def exercise():
            calls, sent = [], []
            async def send(content, key): sent.append(content)
            async def execute(text, sim_round_id):
                task = decode_task(text, sim_round_id, NAMES[0])
                calls.append(task)
                second = int(sim_round_id.split(":")[1])
                return receipt(task, **({"target_position": [second, 40]}
                                        if task["tool"] == "sim_observe" else {}))
            observer = CaptureWindowSession(NAMES[0], "comm", EPISODE, MEMBERS, 5,
                                            None, execute, send, resolve_profile(MEMBERS, pair_id=1))
            for second in range(16):
                for phase in ("begin", "uplink", "observe", "position", "plan", "move", "authorize", "tag", "finish"):
                    await observer.tick(second, phase)
                    await observer.tick(second, phase)
            moves = [t for t in calls if t["tool"] == "sim_move_to"]
            self.assertEqual([t["observed_at"] for t in moves], [0, 5, 10, 15])
            self.assertEqual([t["arguments"] for t in moves], [{"x": t, "y": 40} for t in (0, 5, 10, 15)])
            self.assertEqual(len(observer.outbox), 2)  # t=15 observation and move wait for t=20.
            self.assertEqual(observer.waypoints[NAMES[0]], [15, 40])
            self.assertEqual(len(sent), 6)
        asyncio.run(exercise())

    def test_blind_observer_uses_guidance_then_local_sight_wins_and_loss_allows_guidance(self):
        async def exercise():
            calls = []
            async def send(*args): pass
            async def execute(text, sim_round_id):
                task = decode_task(text, sim_round_id, NAMES[1])
                calls.append(task)
                second = int(sim_round_id.split(":")[1])
                point = [5, 40] if second == 5 else None
                return receipt(task, **({"target_position": point} if task["tool"] == "sim_observe" else {}))
            observer = CaptureWindowSession(NAMES[1], "comm", EPISODE, MEMBERS, 5, None, execute, send)
            for second in range(11):
                for phase in ("begin", "uplink", "observe", "position", "plan"):
                    await observer.tick(second, phase)
                if second in (0, 5, 10):
                    # Valid current contracts may still refer to an older sample.
                    target = 10 if second == 10 else 0
                    task = {"id": "guide-" + str(second), "round_id": f"{EPISODE}:{second}",
                            "agent_id": NAMES[1], "tool": "sim_move_to",
                            "arguments": {"x": target, "y": 40}, "observed_at": target}
                    message = packet(NAMES[2], second, task["id"], "waypoint", [task])
                    message["window_delivery"] = {"receiver": NAMES[1], "sent_at": second,
                                                   "delivered_at": second, "message_id": task["id"]}
                    await observer.receive(message)
                for phase in ("move", "authorize", "tag", "finish"):
                    await observer.tick(second, phase)
            moves = [t for t in calls if t["tool"] == "sim_move_to"]
            self.assertEqual([t["arguments"]["x"] for t in moves], [0, 5, 10])
            self.assertEqual(moves[0]["id"], "guide-0")
            self.assertNotEqual(moves[1]["id"], "guide-5")
            self.assertEqual(moves[2]["id"], "guide-10")
            self.assertTrue(any(e["event"] == "observer_guidance_ignored" for e in observer.events))
            self.assertIsNone(observer.local_observation["target_position"])
        asyncio.run(exercise())

    def test_peer_report_alone_does_not_start_autonomous_observer_motion(self):
        async def exercise():
            async def send(*args): pass
            async def execute(*args, **kwargs): raise AssertionError("peer truth bypassed leader guidance")
            observer = CaptureWindowSession(NAMES[1], "comm", EPISODE, MEMBERS, 5, None, execute, send)
            observer.time, observer.phase = 0, "plan"
            task = {"id": "peer-obs", "round_id": EPISODE + ":0", "agent_id": NAMES[0],
                    "tool": "sim_observe", "arguments": {}}
            observer.record_report(NAMES[0], task, receipt(task))
            await observer.tick(0, "move")
            self.assertIsNone(observer.local_observation)
        asyncio.run(exercise())

    def test_leader_recipient_selection_uses_only_delivered_current_period_reports(self):
        async def exercise():
            contexts, packets = [], []
            class Planner:
                async def agenerate_response(self, **kwargs):
                    ctx = json.loads(kwargs["append_prompt"])
                    contexts.append(ctx)
                    return Message(content={"waypoints": [{"agent_id": n, "x": 0, "y": 40,
                        "observed_at": max(o["observed_at"] for o in ctx["observations"].values())}
                        for n in ctx["waypoint_recipients"]]})
            async def send(content, key): packets.append(content)
            async def execute(*args, **kwargs): raise AssertionError("planner executed a tool")
            leader = CaptureWindowSession(NAMES[2], "comm", EPISODE, MEMBERS, 5, Planner(), execute, send)
            for second, reporters in ((0, [NAMES[0]]), (5, NAMES[:2]), (10, [NAMES[0]])):
                leader.time, leader.phase, leader.command = second, "observe", None
                for name in reporters:
                    observed = {"id": f"{name}-{second}", "round_id": f"{EPISODE}:{second}",
                                "agent_id": name, "tool": "sim_observe", "arguments": {}}
                    leader.record_report(name, observed, receipt(observed))
                await leader.plan()
            self.assertEqual(contexts[0]["waypoint_recipients"], NAMES[1:])
            self.assertEqual(contexts[1]["waypoint_recipients"], NAMES[2:])
            self.assertEqual(contexts[2]["waypoint_recipients"], NAMES[1:])
            # Validation enforces the split even if a model ignores the prompt.
            validate = load_helper("capture_state").validate_waypoint_plan
            for names in (NAMES, NAMES[2:]):
                with self.assertRaisesRegex(ValueError, "exactly waypoint_recipients"):
                    validate(contexts[0], {"waypoints": [{"agent_id": n, "x": 0, "y": 40,
                                                          "observed_at": 0} for n in names]})
        asyncio.run(exercise())


class LeaderObservationStateTests(unittest.TestCase):
    def test_negative_report_replans_and_keeps_last_seen_separate_from_unknown(self):
        async def exercise():
            contexts = []
            class Planner:
                async def agenerate_response(self, **kwargs):
                    context = json.loads(kwargs["append_prompt"])
                    contexts.append(context)
                    sample = max(context["observations"].values(), key=lambda s: s["observed_at"])
                    return Message(content={"waypoints": [
                        {"agent_id": n, "x": sample["target_position"][0], "y": sample["target_position"][1],
                         "observed_at": sample["observed_at"]} for n in context["waypoint_recipients"]]})
            async def send(*args): pass
            async def execute(*args, **kwargs): raise AssertionError("planner executed a tool")
            leader = CaptureWindowSession(NAMES[2], "comm", EPISODE, MEMBERS, 5, Planner(), execute, send)
            for second, point in ((0, [0, 40]), (5, None)):
                leader.time, leader.phase, leader.command = second, "observe", None
                task = {"id": f"seen-{second}", "round_id": f"{EPISODE}:{second}", "agent_id": NAMES[0],
                        "tool": "sim_observe", "arguments": {}}
                leader.record_report(NAMES[0], task, receipt(task, target_position=point), sent_at=second)
                await leader.plan()
            self.assertEqual(len(contexts), 2)  # Negative alone is new planning information.
            state = contexts[-1]["observer_status"]
            self.assertFalse(state[NAMES[0]]["visible_at_latest_sample"])
            self.assertTrue(state[NAMES[0]]["reported_current_sample"])
            self.assertEqual(state[NAMES[0]]["latest_sample"]["observed_at"], 5)
            self.assertEqual(state[NAMES[0]]["last_seen"]["observed_at"], 0)
            self.assertEqual(state[NAMES[0]]["last_seen"]["target_position"], [0, 40])
            self.assertEqual(state[NAMES[0]]["last_seen_age"], 5)
            self.assertIsNone(state[NAMES[1]]["latest_sample"])
            self.assertIsNone(state[NAMES[1]]["visible_at_latest_sample"])
            self.assertEqual(set(contexts[-1]["waypoint_recipients"]), set(NAMES))
            leader.time, leader.command = 6, None
            await leader.plan()
            self.assertEqual(len(contexts), 2)  # No model call just to advance an age.
        asyncio.run(exercise())

    def test_delayed_sighting_does_not_overwrite_newer_negative_and_stale_plan_is_rejected(self):
        async def exercise():
            contexts, sent = [], []
            class Planner:
                async def agenerate_response(self, **kwargs):
                    context, _ = json.JSONDecoder().raw_decode(kwargs["append_prompt"])
                    contexts.append(context)
                    # Deliberately choose an older last-seen sample first.
                    timestamp, point = (10, [10, 40]) if len(contexts) == 1 else (12, [12, 40])
                    return Message(content={"waypoints": [
                        {"agent_id": n, "x": point[0], "y": point[1], "observed_at": timestamp}
                        for n in context["waypoint_recipients"]]})
            async def send(content, key): sent.append(content)
            async def execute(*args, **kwargs): raise AssertionError("planner executed a tool")
            leader = CaptureWindowSession(NAMES[2], "comm", EPISODE, MEMBERS, 5, Planner(), execute, send)
            leader.time, leader.phase = 20, "uplink"
            for name, second, point in ((NAMES[0], 15, None), (NAMES[0], 10, [10, 40]),
                                        (NAMES[0], 5, [5, 40]), (NAMES[1], 12, [12, 40])):
                task = {"id": f"{name}-{second}", "round_id": f"{EPISODE}:{second}", "agent_id": name,
                        "tool": "sim_observe", "arguments": {}}
                message = packet(name, second, task["id"], payload={"contract": task,
                                 "report": receipt(task, target_position=point)})
                message["window_delivery"] = {"receiver": NAMES[2], "sent_at": second, "delivered_at": 20,
                                               "message_id": task["id"]}
                await leader.receive(message)
                await leader.receive(message)
            await leader.plan()
            self.assertEqual(len(contexts), 2)  # Older coordinates were rejected, then corrected.
            state = contexts[-1]["observer_status"]
            self.assertEqual(state[NAMES[0]]["latest_sample"]["observed_at"], 15)
            self.assertFalse(state[NAMES[0]]["visible_at_latest_sample"])
            self.assertEqual(state[NAMES[0]]["latest_sample_sent_at"], 15)
            self.assertEqual(state[NAMES[0]]["latest_sample_received_at"], 20)
            self.assertEqual(state[NAMES[0]]["latest_sample_age"], 5)
            self.assertEqual(state[NAMES[0]]["last_seen"]["observed_at"], 10)
            self.assertEqual(state[NAMES[0]]["last_seen_age"], 10)
            self.assertEqual(state[NAMES[1]]["last_seen"]["observed_at"], 12)
            self.assertFalse(state[NAMES[1]]["reported_current_sample"])
            self.assertTrue(all(action["observed_at"] == 12 for action in leader.desired.values()))
            recorded = [e for e in leader.events if e["event"] == "observation_recorded"]
            self.assertEqual(len(recorded), 4)  # Duplicate delivery does not mutate state.
            self.assertFalse(recorded[1]["latest_sample_updated"])
            self.assertTrue(recorded[1]["last_seen_updated"])
        asyncio.run(exercise())


class HTTPException(RuntimeError):
    def __init__(self, status_code, detail):
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code


def endpoints(path, names, namespace):
    nodes = [n for n in ast.parse(path.read_text()).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes: node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


class FullWindowEpisodeTests(unittest.TestCase):
    def test_all_fixed_scenes_with_changed_teams_continuous_and_restricted(self):
        for number in range(1, 11):
            with self.subTest(scene=number):
                self.run_http_episodes(long_run=False, fixed_number=number)

    def test_continuous_and_all_ten_pairs_with_real_local_tools_and_endpoints(self):
        self.run_http_episodes(long_run=False)

    def test_recovery_across_two_periods_with_real_tools(self):
        self.run_http_episodes(long_run=True)

    def test_initially_blind_observer_receives_guidance_then_follows_with_real_tools(self):
        self.run_http_episodes(long_run=False, blind_observer=True)

    def run_http_episodes(self, long_run, blind_observer=False, fixed_number=None):
        scene_path, _, frozen = fixed_scene(fixed_number) if fixed_number else (None, None, None)
        members = frozen["members"] if frozen else MEMBERS
        auth = {"controller_token": "window-test-controller",
                "agent_tokens": {m["id"]: "test-token-" + m["id"] for m in members}}
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ, {"SIM_URL": "http://test"}):
            scenario = scene_path or ROOT / "simulator/scenarios/five_team.json"
            if long_run or blind_observer:
                # A temporary slow team exercises multiple window cycles while
                # keeping the frozen 3-second hold and all saved scenes intact.
                data = json.loads(scenario.read_text())
                if long_run:
                    data["config"]["agent_speed"] = 0.1
                if blind_observer:
                    for candidate in data["candidates"]:
                        if candidate["id"] == NAMES[1]: candidate["position"] = [30, 40]
                        if candidate["id"] == "F5_UUV_observer": candidate["position"] = [45, 40]
                scenario = Path(output) / "long-test-only.json"
                scenario.write_text(json.dumps(data))
            service = SimulationService(scenario, Path(output), auth)
            server = SimulationHTTPServer(("127.0.0.1", 0), service)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                asyncio.run(self.episodes(ControllerClient(url, auth["controller_token"]), url, auth,
                                         long_run, blind_observer, members, frozen))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    async def episodes(self, controller, url, auth, long_run, blind_observer=False, members=MEMBERS, frozen=None):
        # Local aliases let the production harness exercise each actual team,
        # including all three possible leaders, without changing test globals.
        MEMBERS = members
        NAMES = [m["id"] for m in members]
        ROLES = {m["id"]: m["role"] for m in members}
        queues = {name: asyncio.Queue() for name in NAMES}
        async def deliver(name, message):
            await queues[name].put(message.model_dump())
            await asyncio.sleep(0)
        async def frontend(*args): pass
        server = endpoints(ROOT / "im_server/app.py",
            {"flush_window", "window_session", "configure_window", "control_window", "broadcast_session_message", "window_capabilities"},
            dict(json=json, secrets=secrets, HTTPException=HTTPException, AgentMessage=WireMessage,
                 WindowRouter=WindowRouter, ACK_PREFIX=ACK_PREFIX, window_sessions={},
                 PROTOCOL_VERSION=PROTOCOL_VERSION,
                 session_broadcast_locks=defaultdict(asyncio.Lock), chat_record_manager=CopyBank(),
                 session_manager=SimpleNamespace(sessions={}), send_to_frontend=frontend,
                 connection_manager=SimpleNamespace(send_personal_message=deliver,
                                                   agent_to_websocket={n: True for n in NAMES})))
        class Planner:
            def __init__(self): self.contexts = []
            async def agenerate_response(self, **kwargs):
                context, _ = json.JSONDecoder().raw_decode(kwargs["append_prompt"])
                self.contexts.append(context)
                sample = max(context["observations"].values(), key=lambda s: s["observed_at"])
                return Message(content={"waypoints": [{"agent_id": n, "x": sample["target_position"][0],
                    "y": sample["target_position"][1], "observed_at": sample["observed_at"]}
                    for n in context["waypoint_recipients"]]})

        class Backend:
            def __init__(self, agent, model, index): self.agent, self.model, self.index = agent, model, index
            async def run(self, task_desc, sim_round_id):
                task = decode_task(task_desc, sim_round_id, self.agent.agent_name)
                await asyncio.sleep(self.index * 0.0001)
                self.model.replies.extend([call(task["tool"], **task["arguments"]), call("submit_task", conclusion="完成")])
                uid = uuid.uuid4().hex
                result = await self.agent.run(task_desc, uid, sim_round_id)
                return guard.render_capture_result(result, uid, sim_round_id, self.agent.agent_name)

        planner, clients = Planner(), {}
        runtime = load_runtime({"_listen_message"})
        for index, name in enumerate(NAMES):
            client = AgentClient(url, auth["agent_tokens"][name])
            model = Model([])
            mapping = {"sim_self_state": lambda c=client: json.dumps(c.state()),
                       "sim_observe": lambda c=client: json.dumps(c.observe()),
                       "sim_move_to": lambda episode_id, expected_time, x, y, c=client:
                           json.dumps(c.set_waypoint(episode_id, expected_time, [x, y])),
                       "sim_try_tag": lambda episode_id, expected_time, authorization_id=None, c=client:
                           json.dumps(c.try_tag(episode_id, expected_time, authorization_id))}
            agent = make_agent(model, mapping, agent_name=name, role=ROLES[name])
            layer = runtime["CommunicationLayer"]()
            layer.name, layer.window_sessions, layer.llm = name, {}, planner
            layer.tool_agent, layer.support_nested_teams = Backend(agent, model, index), False
            layer.observation_func = lambda **kwargs: ""
            async def send_wire(data):
                await server["broadcast_session_message"](WireMessage.model_validate_json(data))
            async def receive_wire(n=name):
                return WireMessage.model_validate(await queues[n].get())
            layer.server_websocket = SimpleNamespace(send_message=send_wire, receive_message=receive_wire)
            clients[name] = endpoints(ROOT / "im_client/main.py", {"initialize_window", "control_local_window", "window_capabilities"},
                dict(communicator=layer, os=os, secrets=secrets, HTTPException=HTTPException,
                     PROTOCOL_VERSION=PROTOCOL_VERSION,
                     CaptureWindowSession=CaptureWindowSession, AgentMessage=WireMessage,
                     CommunicationState=SimpleNamespace(DISCUSSION="discussion"),
                     CommunicationType=SimpleNamespace(DISCUSSION="discussion")))

        workers = [asyncio.create_task(clients[n]["communicator"]._listen_message()) for n in NAMES]
        loop = asyncio.get_running_loop()

        async def dispatch(url, body, key):
            parsed = urlsplit(url)
            request = SimpleNamespace(headers={"authorization": "Bearer " + (key or "")})
            if parsed.netloc == "server":
                if parsed.path == "/teamup":
                    comm = uuid.uuid4().hex
                    names = [*body["agent_names"], body["sender"]]
                    server["session_manager"].sessions[comm] = names
                    server["chat_record_manager"][comm] = {"chat_record": []}
                    return {"comm_id": comm, "agent_names": names}
                if parsed.path.endswith("/configure"):
                    return await server["configure_window"](body)
                return await server["control_window"](body, request)
            namespace = clients[parsed.netloc]
            if parsed.path.endswith("/init"):
                return await namespace["initialize_window"](body)
            return await namespace["control_local_window"](body, request)

        def request(url, body, timeout=30, key=None):
            return asyncio.run_coroutine_threadsafe(dispatch(url, body, key), loop).result(timeout)

        try:
            self.assertEqual((await server["window_capabilities"]())["protocol_version"], PROTOCOL_VERSION)
            for namespace in clients.values():
                self.assertEqual((await namespace["window_capabilities"]())["protocol_version"], PROTOCOL_VERSION)
            reopened_episodes, captured_with_backlog, expired_commands = 0, 0, 0
            for pair_id in ([None, 2, 10] if frozen else [None, 1, 5] if blind_observer
                            else [1, 2, 7, 10] if long_run else [None, *range(1, 11)]):
                profile = resolve_profile(MEMBERS, pair_id=pair_id)
                condition, restricted = profile["condition"], profile["restricted"]
                before = len(planner.contexts)
                report = await asyncio.to_thread(run_window_episode, controller,
                    {n: "http://" + n for n in NAMES}, "http://server", expected_members=MEMBERS,
                    config={"pair_id": pair_id}, request=request,
                    print_progress=lambda text: None, round_timeout=15,
                    max_rounds=45 if long_run else 0,
                    expected_scenario=frozen)
                self.assertIsNone(report["error"], report)
                self.assertEqual(report["cleanup_errors"], [])
                if frozen:
                    self.assertIn(report["status"], {"captured", "escaped", "timeout"})
                    self.assertEqual(report["scenario_sha256"], frozen["scenario_sha256"])
                    self.assertEqual(report["scenario_id"], frozen["scenario_id"])
                    self.assertEqual(report["generation"]["seed"], frozen["seed"])
                    self.assertGreater(report["completed_rounds"], 0)
                else:
                    self.assertEqual(report["status"], "aborted" if long_run else "captured")
                if long_run:
                    self.assertEqual(report["time"], 45)
                    self.assertEqual(report["completed_rounds"], 45)
                    self.assertIsNone(report["capture_time"])
                self.assertEqual(report["profile"]["pair_id"], pair_id)
                self.assertEqual(report["experiment_rules"]["observation_period"], 3)
                self.assertFalse(report["experiment_rules"]["support_required"])
                plans = planner.contexts[before:]
                initial_observers = plans[0]["observer_status"]
                expected_recipients = [n for n in NAMES if ROLES[n] != "observer"
                                       or not initial_observers[n]["visible_at_latest_sample"]]
                self.assertEqual(set(plans[0]["waypoint_recipients"]), set(expected_recipients))
                self.assertTrue(any(v["visible_at_latest_sample"] for v in initial_observers.values()))
                if not frozen:
                    self.assertTrue(initial_observers[NAMES[0]]["visible_at_latest_sample"])
                    self.assertEqual(initial_observers[NAMES[1]]["visible_at_latest_sample"], not blind_observer)
                self.assertTrue(initial_observers[NAMES[1]]["reported_current_sample"])
                # Actual local observer actions continue during closure, with
                # real HTTP receipts, without a delivered leader command.
                for name in NAMES[:2]:
                    moves = [c for r in report["rounds"] for c in r["tool_calls"]
                             if c["agent_id"] == name and c["operation"] == "waypoint"]
                    if not frozen and not long_run:
                        self.assertTrue(any(c["time"] == 9 and c["point"] == [9, 40] for c in moves))
                    self.assertEqual(len(moves), len({c["time"] for c in moves}))
                if blind_observer:
                    next_plan = next(p for p in plans if p["time"] == 3)
                    self.assertNotIn(NAMES[1], next_plan["waypoint_recipients"])
                    self.assertTrue(next_plan["observer_status"][NAMES[1]]["visible_at_latest_sample"])
                deliveries = report["transport"]["deliveries"]
                delivered = [row for row in deliveries if row["delivered_at"] is not None]
                self.assertEqual(len(delivered), len({(r["message_id"], r["receiver"]) for r in delivered}))
                for row in delivered:
                    self.assertNotEqual(row["sender"], row["receiver"])
                    self.assertFalse(is_closed(report["profile"], row["sender"], row["server_received_at"]))
                    self.assertFalse(is_closed(report["profile"], row["receiver"], row["delivered_at"]))
                    self.assertLessEqual(row["sent_at"], row["server_received_at"])
                    self.assertLessEqual(row["server_received_at"], row["delivered_at"])
                admitted = {r["message_id"]: r for r in report["transport"]["messages"]}
                for name, local in report["local_events"].items():
                    rows = local["uplink_messages"]
                    self.assertEqual([r["sender_sequence"] for r in rows], list(range(1, len(rows) + 1)))
                    for row in rows:
                        if row["state"] == "queued":
                            self.assertNotIn(row["message_id"], admitted)
                        else:
                            self.assertIn(row["message_id"], admitted)
                            self.assertEqual(row["sent_at"], admitted[row["message_id"]]["sent_at"])
                            self.assertFalse(is_closed(profile, name, row["uplink_sent_at"]))
                # Every actual action is audited even while its group result is queued.
                for row in report["rounds"]:
                    for participant in row["participants"].values():
                        self.assertFalse(participant["busy"])
                        self.assertNotIn("observations", participant)
                        self.assertNotIn("waypoints", participant)
                if pair_id is not None and report["time"] >= 10:
                    self.assertTrue(any(r["queue"]["queued"] > 0 for r in report["rounds"] if 5 <= r["time"] < 10))
                    self.assertTrue(any(r["uplink_queued"] > 0 for r in report["rounds"] if 5 <= r["time"] < 10))
                if condition == "leader_pair":
                    plans = planner.contexts[before:]
                    self.assertFalse(any(is_closed(profile, NAMES[2], p["time"]) for p in plans))
                if pair_id is not None and report["time"] >= 20:
                    reopened_episodes += 1
                    self.assertTrue(any(5 <= r["sent_at"] < 10 and r["delivered_at"] == 10 for r in delivered))
                    events = [e for agent in report["local_events"].values() for e in agent["events"]]
                    expired_commands += sum(e["event"] == "expired_command" for e in events)
                    if long_run:
                        self.assertTrue(any(15 <= r["sent_at"] < 20 and r["delivered_at"] == 20 for r in delivered))
                pending = sum(r["uplink_queued"] for r in report["local_events"].values())
                pending += sum(r["state"] == "queued" for r in deliveries)
                if pending:
                    captured_with_backlog += 1
                    self.assertTrue(report["transport"]["closed"])
                if report["capture_time"] is not None and is_closed(profile, NAMES[2], report["capture_time"]):
                    self.assertIsNone(report["leader_capture_known_at"])
                    self.assertFalse(report["leader_informed_at_end"])
                with self.assertRaisesRegex(HTTPException, "403"):
                    await dispatch("http://server/simulation/window/control",
                                   {"comm_id": report["comm_id"], "operation": "export"}, "wrong-key")
            if long_run:
                self.assertGreater(reopened_episodes, 0)
                self.assertGreater(expired_commands, 0)
            elif not frozen:
                self.assertGreater(captured_with_backlog, 0)
            async def invalid_executor(*args, **kwargs): return "任务完成，但没有工具回执"
            clients[NAMES[0]]["communicator"].tool_agent_run = invalid_executor
            failed = await asyncio.to_thread(run_window_episode, controller,
                {n: "http://" + n for n in NAMES}, "http://server", expected_members=MEMBERS,
                request=request, print_progress=lambda text: None, round_timeout=15)
            self.assertIn("no executor receipt", failed["error"])
            self.assertEqual(failed["status"], "aborted")
            self.assertEqual(failed["time"], 0)
            self.assertEqual(failed["completed_rounds"], 0)
        finally:
            for worker_task in workers: worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
