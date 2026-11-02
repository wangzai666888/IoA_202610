"""Frozen authorization policy, real delivery evidence and physical adjudication."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from common.communication_windows import WindowRouter, resolve_profile, MESSAGE_PREFIX
from simulator.core import AgentState, CaptureEnvironment, SimConfig
from simulator.experiment_rules import RULES, frozen_rules, validate_authorization
from simulator.ioa_run import verify_round_audit
from simulator.service import SimulationService, APIError
from simulator.authorization_metrics import authorization_timeline
from simulator.tests.test_communication_windows import packet, flush, advance, CaptureWindowSession
from simulator.tests.test_capture_phases import EPISODE, MEMBERS, NAMES, ROLES, receipt
from common.capture_protocol import decode_task


def grant(issued=0, marker=NAMES[3], **changes):
    row = {"authorization_id": "grant-" + str(issued), "episode_id": EPISODE,
           "leader_id": NAMES[2], "marker_id": marker, "observer_id": NAMES[0],
           "observed_at": issued, "target_position": [0, 40],
           "position_at": issued, "marker_position": [0, 40], "issued_at": issued,
           "expires_at": issued + 5, "estimated_distance": 0.0, "rules_version": RULES["version"]}
    return row | changes


def environment():
    return CaptureEnvironment(
        SimConfig(observation_period=3, support_required=False, authorization_required=True),
        [AgentState(NAMES[0], "UAV", "observer", (0, 64)),
         AgentState(NAMES[1], "USV", "observer", (90, 90)),
         AgentState(NAMES[2], "UUV", "leader", (90, 90)),
         AgentState(NAMES[3], "USV", "marker", (0, 40)),
         AgentState(NAMES[4], "UAV", "marker", (0, 48))],
        [(0, 40), (12, 40), (12, 55), (101, 55)], experiment_rules=frozen_rules())


def register(env, row, delivered=0):
    env.register_authorization_delivery({"grant": row, "message_id": row["authorization_id"],
                                         "delivered_at": delivered})


class PhysicalAuthorizationTests(unittest.TestCase):
    def test_three_completed_seconds_without_observer_or_leader_support(self):
        env = environment()
        register(env, grant())
        for t in range(3):
            self.assertEqual(env.time, t)
            result = env.try_tag(NAMES[3], "grant-0")
            self.assertFalse(result["accepted"])
            self.assertEqual(result["reason"], "marker_hold_incomplete")
            env.advance_one_second()
        self.assertEqual(env.status, "running")
        self.assertTrue(env.try_tag(NAMES[3], "grant-0")["accepted"])
        self.assertEqual(env.capture_time, 3)

    def test_two_markers_cannot_pool_time_and_leaving_resets(self):
        env = environment()
        register(env, grant())
        register(env, grant(marker=NAMES[4], authorization_id="other-marker"))
        for _ in range(2):
            env.advance_one_second()
        self.assertEqual(env._support_streak[NAMES[3]], 2)
        env._agents[NAMES[3]].pos = (90, 90)
        env._agents[NAMES[4]].pos = (2, 40)
        env.advance_one_second()
        self.assertEqual(env._support_streak[NAMES[3]], 0)
        self.assertEqual(env._support_streak[NAMES[4]], 1)
        self.assertEqual(env.try_tag(NAMES[3], "grant-0")["reason"], "marker_out_of_range")
        self.assertEqual(env.try_tag(NAMES[4], "other-marker")["reason"], "marker_hold_incomplete")
        self.assertFalse(env.try_tag(NAMES[4], "grant-0")["authorization_valid"])

    def test_radius_inclusive_and_counter_independent_of_authorization(self):
        env = environment()
        for _ in range(5):
            env.advance_one_second()
        self.assertEqual(env._support_streak[NAMES[3]], 5)  # exactly 5m still counts
        self.assertFalse(env.try_tag(NAMES[3])["accepted"])
        self.assertEqual(env.try_tag(NAMES[3])["reason"], "authorization_not_delivered")
        register(env, grant(5), delivered=5)
        self.assertTrue(env.try_tag(NAMES[3], "grant-5")["accepted"])

    def test_late_delivery_never_extends_expiry(self):
        env = environment()
        for _ in range(5):
            env.advance_one_second()
        register(env, grant(), delivered=5)
        result = env.try_tag(NAMES[3], "grant-0")
        self.assertEqual(result["reason"], "authorization_expired")
        self.assertFalse(result["authorization_valid"])
        self.assertEqual(env.status, "running")

    def test_frozen_freshness_boundary_and_reported_distance(self):
        validate_authorization(grant(5, observed_at=0, position_at=0), EPISODE, ROLES)
        for changes in ({"observed_at": 0}, {"position_at": 0}, {"expires_at": 12},
                        {"marker_position": [6, 40], "estimated_distance": 6}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_authorization(grant(6, **changes), EPISODE, ROLES)


class AgentAuthorizationTests(unittest.TestCase):
    def session(self, name, pair=None):
        sent, tasks = [], []
        async def send(content, round_id):
            sent.append(json.loads(content[len(MESSAGE_PREFIX):]) if content.startswith(MESSAGE_PREFIX) else content)
        async def execute(text, sim_round_id):
            task = decode_task(text, sim_round_id, name)
            tasks.append(task)
            return receipt(task)
        session = CaptureWindowSession(name, "comm", EPISODE, MEMBERS, 3, None, execute, send,
                                        resolve_profile(MEMBERS, pair_id=pair))
        return session, sent, tasks

    def report(self, session, name, second, tool, **extra):
        task = {"id": f"{name}-{second}-{tool}", "round_id": f"{EPISODE}:{second}",
                "agent_id": name, "tool": tool, "arguments": {}}
        session.record_report(name, task, receipt(task, **extra), sent_at=second)

    def test_known_reports_negative_samples_freshness_and_no_automatic_renewal(self):
        async def run():
            leader, sent, _ = self.session(NAMES[2])
            leader.time = 5
            self.report(leader, NAMES[0], 0, "sim_observe")
            self.report(leader, NAMES[3], 0, "sim_self_state")
            await leader.authorize()
            self.assertEqual(len(leader.issued_grants), 1)
            self.assertEqual(leader.issued_grants[NAMES[3]]["expires_at"], 10)
            for t in (6, 7, 8, 9):
                leader.time = t
                await leader.authorize()
            self.assertEqual(sum(isinstance(p, dict) and p["kind"] == "authorization" for p in sent), 1)
            leader.time = 10
            await leader.authorize()  # stale reports cannot renew
            self.assertEqual(leader.issued_grants[NAMES[3]]["issued_at"], 5)
            self.report(leader, NAMES[0], 9, "sim_observe", target_position=None)
            self.report(leader, NAMES[0], 6, "sim_observe")  # late positive after a newer loss
            self.report(leader, NAMES[3], 10, "sim_self_state")
            await leader.authorize()
            self.assertEqual(leader.issued_grants[NAMES[3]]["issued_at"], 5)
            self.report(leader, NAMES[1], 9, "sim_observe")  # observer handoff is allowed
            await leader.authorize()
            renewed = leader.issued_grants[NAMES[3]]
            self.assertEqual(renewed["issued_at"], 10)
            self.assertEqual(renewed["observer_id"], NAMES[1])
            self.assertEqual(renewed["observed_at"], 9)
        asyncio.run(run())

    def test_received_grant_works_while_closed_but_stops_at_exact_expiry(self):
        async def run():
            marker, _, tasks = self.session(NAMES[3], pair=10)
            marker.time, marker.phase = 4, "authorize"
            message = packet(NAMES[2], 4, "grant-message", "authorization", grant(4))
            message["window_delivery"] = {"receiver": NAMES[3], "sent_at": 4,
                                            "delivered_at": 4, "message_id": "grant-message"}
            await marker.receive(message)
            await marker.receive(message)
            for t in (5, 6, 7, 8, 9):
                marker.time, marker.phase = t, "authorize"
                marker.expire_authorizations()
                await marker.tick(t, "tag")
            self.assertEqual([int(t["round_id"].split(":")[1]) for t in tasks], [5, 6, 7, 8])
            self.assertTrue(all(t["authorization_id"] == "grant-4" for t in tasks))
            self.assertEqual(len([e for e in marker.events if e["event"] == "authorization_expired"]), 1)
        asyncio.run(run())

    def test_no_grant_and_expired_delivery_do_not_start_tool(self):
        async def run():
            marker, _, tasks = self.session(NAMES[3])
            marker.time = 10
            await marker.tag()
            message = packet(NAMES[2], 5, "old", "authorization", grant(5))
            message["window_delivery"] = {"receiver": NAMES[3], "sent_at": 5,
                                            "delivered_at": 10, "message_id": "old"}
            await marker.receive(message)
            await marker.tag()
            self.assertEqual(tasks, [])
            self.assertTrue(any(e["event"] == "authorization_expired" for e in marker.events))
        asyncio.run(run())

    def test_old_position_delivery_does_not_replace_newer_report(self):
        leader, _, _ = self.session(NAMES[2])
        leader.time = 10
        self.report(leader, NAMES[3], 9, "sim_self_state", position=[1, 40])
        self.report(leader, NAMES[3], 5, "sim_self_state", position=[90, 90])
        self.assertEqual(leader.positions[NAMES[3]]["position_at"], 9)
        self.assertEqual(leader.positions[NAMES[3]]["position"], [1, 40])


class DeliveryAuditTests(unittest.TestCase):
    def test_timeline_distinguishes_pending_expired_delivery_and_failed_attempt(self):
        first, pending = grant(0), grant(5)
        rows = [{"message_id": "m0", "sent_at": 0, "uplink_sent_at": 0,
                 "content": packet(NAMES[2], 0, "m0", "authorization", first)["content"]},
                {"message_id": "m5", "sent_at": 5, "uplink_sent_at": None,
                 "content": packet(NAMES[2], 5, "m5", "authorization", pending)["content"]}]
        transport = {"messages": [{"message_id": "m0", "server_received_at": 0}],
                     "deliveries": [{"message_id": "m0", "receiver": NAMES[3],
                                     "delivered_at": 1, "state": "acknowledged"}]}
        attempts = [{"operation": "tag", "authorization_id": first["authorization_id"],
                     "agent_id": NAMES[3], "time": 1, "authorization_valid": True,
                     "accepted": False, "reason": "marker_hold_incomplete"}]
        timeline = authorization_timeline(transport, {NAMES[2]: {"uplink_messages": rows}}, attempts, 7)
        self.assertEqual(timeline[0]["downlink_wait_seconds"], 1)
        self.assertTrue(timeline[0]["expired_by_end"])
        self.assertFalse(timeline[0]["expired_on_delivery"])
        self.assertEqual(timeline[0]["attempts"][0]["reason"], "marker_hold_incomplete")
        self.assertIsNone(timeline[1]["delivered_at"])
        self.assertFalse(timeline[1]["expired_by_end"])
        transport["deliveries"][0]["delivered_at"] = 5
        late = authorization_timeline(transport, {NAMES[2]: {"uplink_messages": rows}}, [], 10)
        self.assertTrue(late[0]["expired_on_delivery"])
        self.assertTrue(late[1]["expired_by_end"])

    def test_router_requires_actual_marker_ack_and_preserves_expired_grant(self):
        router = WindowRouter(EPISODE, "comm", resolve_profile(MEMBERS, pair_id=10))
        advance(router, 5)
        router.accept(packet(NAMES[2], 5, "a", "authorization", grant(5)), NAMES[2])
        flush(router)
        self.assertEqual(router.authorization_receipts(), [])
        advance(router, 10)
        ready = router.ready()
        for row in ready:
            router.sent(row)
        self.assertEqual(router.authorization_receipts(), [])  # socket send is not an ACK
        for row in ready:
            router.acknowledge(row["receiver"], row["message_id"])
        receipts = router.authorization_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["delivered_at"], 10)
        self.assertEqual(receipts[0]["grant"]["expires_at"], 10)

    def test_audit_allows_no_tag_but_rejects_forged_expired_and_undelivered_attempts(self):
        audit = {"episode_id": EPISODE, "time": 4, "status": "running", "experiment_rules": frozen_rules(),
                 "calls": [{"agent_id": n, "time": 4, "operation": "self_state"} for n in NAMES[3:]],
                 "authorization_deliveries": []}
        self.assertEqual(verify_round_audit(audit, EPISODE, 4, 3, MEMBERS), [])
        audit["calls"].append({"agent_id": NAMES[3], "time": 4, "operation": "tag", "accepted": False,
                               "authorization_id": "g", "authorization_valid": True})
        self.assertTrue(verify_round_audit(audit, EPISODE, 4, 3, MEMBERS))
        audit["authorization_deliveries"] = [{"authorization_id": "g", "marker_id": NAMES[3],
                                               "issued_at": 0, "delivered_at": 0, "expires_at": 5}]
        self.assertEqual(verify_round_audit(audit, EPISODE, 4, 3, MEMBERS), [])
        for change in ({"delivered_at": 5}, {"expires_at": 4}, {"marker_id": NAMES[4]}):
            modified = deepcopy(audit)
            modified["authorization_deliveries"][0].update(change)
            self.assertTrue(verify_round_audit(modified, EPISODE, 4, 3, MEMBERS))

    def test_runtime_rules_preserve_scene_hash_and_registration_is_controller_only(self):
        scene = Path(__file__).parents[1] / "scenarios/five_team.json"
        auth = {"controller_token": "controller-test-secret", "agent_tokens": {n: "token-" + n for n in NAMES}}
        raw = scene.read_bytes()
        with tempfile.TemporaryDirectory() as output:
            service = SimulationService(scene, Path(output), auth)
            control = "Bearer " + auth["controller_token"]
            state = service.dispatch("POST", "/control/reset", control, {"experiment_rules": frozen_rules()})
            self.assertEqual(state["observation_period"], 3)
            self.assertEqual(scene.read_bytes(), raw)
            self.assertFalse(service.env.config.support_required)
            with self.assertRaises(APIError) as error:
                service.dispatch("POST", "/control/authorizations", "Bearer " + auth["agent_tokens"][NAMES[3]], {})
            self.assertEqual(error.exception.status, 403)
            wrong = {"episode_id": state["episode_id"], "expected_time": 0,
                     "receipts": [{"grant": grant(), "message_id": "x", "delivered_at": 0}]}
            with self.assertRaisesRegex(APIError, "another episode"):
                service.dispatch("POST", "/control/authorizations", control, wrong)


if __name__ == "__main__":
    unittest.main()
