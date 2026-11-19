"""DynamoStore and SqsScheduler against hand-written stubs. No boto3, no network.

The stub table keeps items in a dict and evaluates the handful of condition
and update expressions the adapter uses, so these tests check what the
expressions do, not just that they were sent.
"""
import json
import re
import unittest
from decimal import Decimal

from support import PRIMARY

from er.aws_adapters import POINTER_KEY, DynamoStore, SqsScheduler
from er.config import Config
from er.models import ACKNOWLEDGED, ACTIVE, CANCELLED, Incident

TABLE = "incidents"
NOW = 1_000_000


class ConditionalCheckFailedException(Exception):
    pass


class TransactionCanceledException(Exception):
    pass


class StubExceptions:
    ConditionalCheckFailedException = ConditionalCheckFailedException
    TransactionCanceledException = TransactionCanceledException


def condition_holds(expression, item, names, values):
    """Evaluates `a = :v`, `a < :v` and attribute_not_exists(a), joined by AND / OR.
    As in DynamoDB, comparing a missing attribute is false."""
    if not expression:
        return True

    def term(text):
        text = text.strip()
        missing = re.match(r"^attribute_not_exists\((\S+)\)$", text)
        if missing:
            return item is None or names.get(missing.group(1), missing.group(1)) not in item
        present = re.match(r"^attribute_exists\((\S+)\)$", text)
        if present:
            return item is not None and names.get(present.group(1), present.group(1)) in item
        left, op, right = re.match(r"^(\S+) (=|<) (:\w+)$", text).groups()
        field = names.get(left, left)
        if item is None or field not in item:
            return False
        return item[field] == values[right] if op == "=" else item[field] < values[right]

    return any(
        all(term(t) for t in alternative.split(" AND "))
        for alternative in expression.split(" OR ")
    )


def as_dynamo(item):
    """DynamoDB hands numbers back as Decimal."""
    return {k: Decimal(v) if isinstance(v, int) else v for k, v in item.items()}


class StubTable:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def get_item(self, Key, ConsistentRead=False):
        self.calls.append(("get_item", {"Key": Key, "ConsistentRead": ConsistentRead}))
        item = self.items.get(Key["pk"])
        return {"Item": as_dynamo(item)} if item is not None else {}

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))
        pk = kwargs["Key"]["pk"]
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        if not condition_holds(kwargs.get("ConditionExpression"), self.items.get(pk), names, values):
            raise ConditionalCheckFailedException()
        item = self.items.setdefault(pk, {"pk": pk})
        parts = re.match(r"^(?:SET (.*?))?\s*(?:REMOVE (.*))?$", kwargs["UpdateExpression"]).groups()
        for assignment in (parts[0] or "").split(", "):
            if assignment:
                name, value = assignment.split(" = ")
                item[names[name]] = values[value]
        for name in (parts[1] or "").split(", "):
            if name:
                item.pop(names[name], None)

    def delete_item(self, **kwargs):
        self.calls.append(("delete_item", kwargs))
        pk = kwargs["Key"]["pk"]
        if not condition_holds(
            kwargs.get("ConditionExpression"), self.items.get(pk), {},
            kwargs.get("ExpressionAttributeValues", {}),
        ):
            raise ConditionalCheckFailedException()
        self.items.pop(pk, None)


class StubClient:
    exceptions = StubExceptions

    def __init__(self, items):
        self.items = items
        self.transactions = []

    def transact_write_items(self, TransactItems):
        self.transactions.append(TransactItems)
        puts = [entry["Put"] for entry in TransactItems]
        for put in puts:  # all conditions are checked before anything is written
            if not condition_holds(
                put.get("ConditionExpression"), self.items.get(put["Item"]["pk"]), {},
                put.get("ExpressionAttributeValues", {}),
            ):
                raise TransactionCanceledException()
        for put in puts:
            self.items[put["Item"]["pk"]] = dict(put["Item"])


class StubMeta:
    def __init__(self, client):
        self.client = client


class StubResource:
    def __init__(self):
        self.items = {}
        self.table = StubTable(self.items)
        self.meta = StubMeta(StubClient(self.items))

    def Table(self, name):
        assert name == TABLE
        return self.table


def incident(incident_id, created_at=NOW):
    return Incident(
        id=incident_id, sender="Robin", kind="kids", created_at=created_at,
        base_url="https://example.invalid",
    )


class DynamoStoreTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(primary_number=PRIMARY)
        self.resource = StubResource()
        self.items = self.resource.items
        self.table = self.resource.table
        self.store = DynamoStore(TABLE, self.cfg, resource=self.resource)

    def create(self, incident_id, now=NOW):
        return self.store.create_if_none_active(incident(incident_id, now), now)

    # ------------------------------------------------------------------ pause

    def test_pause_round_trip(self):
        self.assertEqual(self.store.pause_state(), (0, ""))
        self.store.set_pause(NOW + 600, "Robin")
        self.assertEqual(self.store.pause_state(), (NOW + 600, "Robin"))
        self.store.set_pause(0)
        self.assertEqual(self.store.pause_state(), (0, ""))

    def test_active_id_follows_the_pointer(self):
        self.assertIsNone(self.store.active_id())
        self.create("incident-aaa")
        self.assertEqual(self.store.active_id(), "incident-aaa")
        self.store.transition("incident-aaa", "active", "cancelled")
        self.assertIsNone(self.store.active_id())

    # ------------------------------------------------- create_if_none_active

    def test_create_writes_incident_and_pointer_in_one_transaction(self):
        created, was_created = self.create("incident-aaa")
        self.assertTrue(was_created)
        self.assertEqual(created.id, "incident-aaa")
        self.assertEqual(len(self.resource.meta.client.transactions), 1)
        self.assertEqual(len(self.resource.meta.client.transactions[0]), 2)
        self.assertEqual(
            self.items[POINTER_KEY],
            {"pk": POINTER_KEY, "incident_id": "incident-aaa",
             "expires_at": NOW + self.cfg.active_pointer_ttl},
        )
        record = self.items["INC#incident-aaa"]
        self.assertEqual(record["status"], ACTIVE)
        self.assertEqual(record["ttl"], NOW + self.cfg.record_ttl)
        self.assertEqual((record["tick_seq"], record["attempts"]), (0, 0))

    def test_second_create_returns_the_active_incident(self):
        self.create("incident-aaa")
        existing, was_created = self.create("incident-bbb", NOW + 30)
        self.assertFalse(was_created)
        self.assertEqual(existing.id, "incident-aaa")
        self.assertNotIn("INC#incident-bbb", self.items)
        self.assertEqual(self.items[POINTER_KEY]["incident_id"], "incident-aaa")

    def test_stale_pointer_does_not_block_a_new_incident(self):
        self.create("incident-aaa")
        later = NOW + self.cfg.active_pointer_ttl + 1
        created, was_created = self.create("incident-bbb", later)
        self.assertTrue(was_created)
        self.assertEqual(created.id, "incident-bbb")
        self.assertEqual(self.items[POINTER_KEY]["incident_id"], "incident-bbb")
        self.assertEqual(
            self.items[POINTER_KEY]["expires_at"], later + self.cfg.active_pointer_ttl
        )

    def test_pointer_is_not_stale_at_exactly_its_expiry(self):
        self.create("incident-aaa")
        _, was_created = self.create("incident-bbb", NOW + self.cfg.active_pointer_ttl)
        self.assertFalse(was_created)

    def test_pointer_left_on_a_closed_incident_does_not_block_a_new_one(self):
        # The status change succeeded but the pointer delete never ran.
        self.create("incident-aaa")
        self.items["INC#incident-aaa"]["status"] = ACKNOWLEDGED
        created, was_created = self.create("incident-bbb", NOW + 30)
        self.assertTrue(was_created)
        self.assertEqual(created.id, "incident-bbb")
        self.assertEqual(self.items[POINTER_KEY]["incident_id"], "incident-bbb")

    def test_pointer_to_a_missing_incident_does_not_block_a_new_one(self):
        self.items[POINTER_KEY] = {
            "pk": POINTER_KEY, "incident_id": "incident-gone", "expires_at": NOW + 999,
        }
        _, was_created = self.create("incident-bbb")
        self.assertTrue(was_created)

    # ------------------------------------------------------------------- get

    def test_get_converts_decimals_and_uses_consistent_reads(self):
        self.create("incident-aaa")
        loaded = self.store.get("incident-aaa")
        self.assertIs(type(loaded.created_at), int)
        self.assertIs(type(loaded.tick_seq), int)
        self.assertEqual(loaded, incident("incident-aaa"))
        self.assertEqual(
            self.table.calls[-1],
            ("get_item", {"Key": {"pk": "INC#incident-aaa"}, "ConsistentRead": True}),
        )
        self.assertIsNone(self.store.get("incident-zzz"))

    # ---------------------------------------------------- conditional updates

    def test_transition_changes_status_and_sets_fields(self):
        self.create("incident-aaa")
        ok = self.store.transition(
            "incident-aaa", ACTIVE, ACKNOWLEDGED, acked_via="call", acked_at=NOW + 5
        )
        self.assertTrue(ok)
        record = self.items["INC#incident-aaa"]
        self.assertEqual(record["status"], ACKNOWLEDGED)
        self.assertEqual((record["acked_via"], record["acked_at"]), ("call", NOW + 5))

    def test_transition_returns_false_when_status_differs(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.transition("incident-aaa", ACTIVE, ACKNOWLEDGED))
        self.assertFalse(self.store.transition("incident-aaa", ACTIVE, CANCELLED, closed_at=1))
        record = self.items["INC#incident-aaa"]
        self.assertEqual(record["status"], ACKNOWLEDGED)
        self.assertNotIn("closed_at", record)

    def test_transition_on_missing_incident_returns_false_and_creates_nothing(self):
        self.assertFalse(self.store.transition("incident-zzz", ACTIVE, CANCELLED))
        self.assertEqual(self.items, {})

    def test_advance_tick_only_from_the_expected_seq(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.advance_tick("incident-aaa", 0, 1000))
        self.assertEqual(self.items["INC#incident-aaa"]["tick_seq"], 1)
        self.assertFalse(self.store.advance_tick("incident-aaa", 0, 1000))  # duplicate delivery
        self.assertEqual(self.items["INC#incident-aaa"]["tick_seq"], 1)

    def test_advance_tick_returns_false_once_closed(self):
        self.create("incident-aaa")
        self.store.transition("incident-aaa", ACTIVE, CANCELLED)
        self.assertFalse(self.store.advance_tick("incident-aaa", 0, 1000))

    def test_claim_attempt_only_from_the_expected_count(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.claim_attempt("incident-aaa", 0, NOW + 1))
        record = self.items["INC#incident-aaa"]
        self.assertEqual((record["attempts"], record["call_in_flight_since"]), (1, NOW + 1))
        self.assertFalse(self.store.claim_attempt("incident-aaa", 0, NOW + 2))  # lost the race
        self.assertEqual((record["attempts"], record["call_in_flight_since"]), (1, NOW + 1))

    def test_claim_attempt_returns_false_once_closed(self):
        self.create("incident-aaa")
        self.store.transition("incident-aaa", ACTIVE, ACKNOWLEDGED)
        self.assertFalse(self.store.claim_attempt("incident-aaa", 0, NOW))

    # -------------------------------------------------------- pointer release

    def test_closing_releases_the_pointer(self):
        self.create("incident-aaa")
        self.store.transition("incident-aaa", ACTIVE, ACKNOWLEDGED)
        self.assertNotIn(POINTER_KEY, self.items)
        _, was_created = self.create("incident-bbb", NOW + 30)
        self.assertTrue(was_created)

    def test_failed_transition_leaves_the_pointer(self):
        self.create("incident-aaa")
        self.assertFalse(self.store.transition("incident-aaa", CANCELLED, ACKNOWLEDGED))
        self.assertEqual(self.items[POINTER_KEY]["incident_id"], "incident-aaa")

    def test_closing_an_old_incident_leaves_a_newer_pointer_alone(self):
        self.create("incident-aaa")
        self.create("incident-bbb", NOW + self.cfg.active_pointer_ttl + 1)  # takes over
        self.assertTrue(self.store.transition("incident-aaa", ACTIVE, CANCELLED))
        self.assertEqual(self.items[POINTER_KEY]["incident_id"], "incident-bbb")

    # ---------------------------------------------------------------- update

    def test_update_sets_fields(self):
        self.create("incident-aaa")
        self.store.update("incident-aaa", last_call_sid="CA1", pushover_receipt="r1")
        record = self.items["INC#incident-aaa"]
        self.assertEqual((record["last_call_sid"], record["pushover_receipt"]), ("CA1", "r1"))

    def test_update_with_none_removes_the_field(self):
        self.create("incident-aaa")
        self.store.claim_attempt("incident-aaa", 0, NOW)
        self.store.update("incident-aaa", call_in_flight_since=None)
        self.assertNotIn("call_in_flight_since", self.items["INC#incident-aaa"])
        _, kwargs = self.table.calls[-1]
        self.assertEqual(kwargs["UpdateExpression"], "REMOVE #f0")
        self.assertNotIn("ExpressionAttributeValues", kwargs)  # DynamoDB rejects an empty map
        self.assertIsNone(self.store.get("incident-aaa").call_in_flight_since)

    def test_update_can_set_and_remove_together(self):
        self.create("incident-aaa")
        self.store.claim_attempt("incident-aaa", 0, NOW)
        self.store.update("incident-aaa", call_in_flight_since=None, last_call_sid="CA2")
        record = self.items["INC#incident-aaa"]
        self.assertNotIn("call_in_flight_since", record)
        self.assertEqual(record["last_call_sid"], "CA2")
        _, kwargs = self.table.calls[-1]
        self.assertEqual(kwargs["UpdateExpression"], "SET #f1 = :v1 REMOVE #f0")


class StubSqs:
    def __init__(self):
        self.sent = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)


class SlotTests(unittest.TestCase):
    """Break-glass limits and idempotency records."""

    def setUp(self):
        resource = StubResource()
        self.items = resource.items
        self.table = resource.table
        self.store = DynamoStore(TABLE, Config(primary_number=PRIMARY), resource=resource)

    def test_first_claim_wins_and_the_next_reports_time_left(self):
        self.assertEqual(self.store.claim_slot("bg:Assistant", NOW, 900, 8), (True, 0, ""))
        item = self.items["COOLDOWN#bg:Assistant"]
        self.assertEqual((item["free_at"], item["sent"], item["ver"]), (NOW + 900, [NOW], 1))
        self.assertEqual(self.store.claim_slot("bg:Assistant", NOW + 300, 900, 8), (False, 600, "interval"))
        # A refused claim leaves the record as it was.
        self.assertEqual(self.items["COOLDOWN#bg:Assistant"]["ver"], 1)

    def test_claimable_again_exactly_when_the_interval_ends(self):
        self.store.claim_slot("k", NOW, 900, 0)
        self.assertEqual(self.store.claim_slot("k", NOW + 899, 900, 0), (False, 1, "interval"))
        self.assertEqual(self.store.claim_slot("k", NOW + 900, 900, 0), (True, 0, ""))

    def test_daily_cap_is_a_rolling_24_hours(self):
        for i in range(3):
            self.assertTrue(self.store.claim_slot("k", NOW + i * 1000, 900, 3)[0])
        taken, wait, reason = self.store.claim_slot("k", NOW + 3000, 900, 3)
        self.assertEqual((taken, reason), (False, "daily_cap"))
        self.assertEqual(wait, 86400 - 3000)          # until the oldest one ages out
        self.assertTrue(self.store.claim_slot("k", NOW + 86400, 900, 3)[0])
        self.assertEqual(len(self.items["COOLDOWN#k"]["sent"]), 3)   # old entries pruned

    def test_peek_takes_nothing(self):
        self.assertEqual(self.store.peek_slot("k", NOW, 8), (0, "", 0))
        self.assertNotIn("COOLDOWN#k", self.items)
        self.store.claim_slot("k", NOW, 900, 8)
        self.assertEqual(self.store.peek_slot("k", NOW + 100, 8), (800, "interval", 1))
        self.assertEqual(self.items["COOLDOWN#k"]["ver"], 1)

    def test_release_gives_the_slot_back(self):
        self.store.claim_slot("k", NOW, 900, 8)
        self.store.release_slot("k", NOW)
        self.assertEqual(self.store.peek_slot("k", NOW + 1, 8), (0, "", 0))
        self.assertEqual(self.store.claim_slot("k", NOW + 1, 900, 8), (True, 0, ""))

    def test_a_concurrent_writer_makes_the_claim_reread(self):
        self.store.claim_slot("k", NOW, 900, 8)
        original = self.table.get_item

        def racing_get(Key, ConsistentRead=False):
            reply = original(Key=Key, ConsistentRead=ConsistentRead)
            # Someone else takes the slot between our read and our write.
            self.items[Key["pk"]].update(ver=self.items[Key["pk"]]["ver"] + 1, free_at=NOW + 5000)
            self.table.get_item = original
            return reply

        self.table.get_item = racing_get
        self.assertEqual(self.store.claim_slot("k", NOW + 1000, 900, 8), (False, 4000, "interval"))

    def test_keys_are_independent_and_rows_expire(self):
        self.assertTrue(self.store.claim_slot("a", NOW, 900, 8)[0])
        self.assertTrue(self.store.claim_slot("b", NOW, 900, 8)[0])
        self.assertGreater(self.items["COOLDOWN#a"]["ttl"], NOW + 86400)

    def test_idempotency_record_lifecycle(self):
        self.assertIsNone(self.store.idem_begin("bg:Assistant:evt-1", NOW))
        self.assertEqual(self.store.idem_begin("bg:Assistant:evt-1", NOW + 1), {"state": "pending"})
        self.store.idem_finish("bg:Assistant:evt-1", {"delivered": True, "retry_after_seconds": 900})
        self.assertEqual(
            self.store.idem_begin("bg:Assistant:evt-1", NOW + 2),
            {"state": "done", "result": {"delivered": True, "retry_after_seconds": 900}},
        )
        self.assertGreater(self.items["IDEM#bg:Assistant:evt-1"]["ttl"], NOW)
        # Forgetting a key lets it be used again.
        self.store.idem_finish("bg:Assistant:evt-1", None)
        self.assertIsNone(self.store.idem_begin("bg:Assistant:evt-1", NOW + 3))


class SqsSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.sqs = StubSqs()
        self.scheduler = SqsScheduler("https://sqs.invalid/queue", client=self.sqs)

    def test_tick_message(self):
        self.scheduler.schedule_tick("incident-aaa", 3, 60)
        self.assertEqual(len(self.sqs.sent), 1)
        message = self.sqs.sent[0]
        self.assertEqual(message["QueueUrl"], "https://sqs.invalid/queue")
        self.assertEqual(message["DelaySeconds"], 60)
        # The shape handler.lambda_handler reads back out of the SQS record.
        self.assertEqual(json.loads(message["MessageBody"]), {"incident_id": "incident-aaa", "seq": 3})

    def test_push_message(self):
        self.scheduler.schedule_push("incident-aaa", 30)
        message = self.sqs.sent[0]
        self.assertEqual(message["DelaySeconds"], 30)
        self.assertEqual(
            json.loads(message["MessageBody"]), {"incident_id": "incident-aaa", "action": "push"}
        )

    def test_delay_is_kept_inside_what_sqs_accepts(self):
        self.scheduler.schedule_tick("incident-aaa", 0, 5000)
        self.scheduler.schedule_tick("incident-aaa", 1, -5)
        self.assertEqual([m["DelaySeconds"] for m in self.sqs.sent], [900, 0])


if __name__ == "__main__":
    unittest.main()
