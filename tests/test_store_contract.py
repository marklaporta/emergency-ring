"""One contract for the incident store, run against both implementations.

Core is tested on InMemoryStore and deployed on DynamoStore, so the two have
to agree wherever core can tell them apart. StoreContract states that
behaviour once, through the methods in ports.Store, and runs it on each.

DynamoStore runs on the stub table from test_aws_adapters, made stricter
here: it hands values back the way DynamoDB does and refuses requests that
DynamoDB would refuse. The stub still interprets the expressions itself, so a
mistake that the adapter and the stub share only shows up on AWS.
"""
import itertools
import os
import re
import unittest
from decimal import Decimal
from unittest import mock

from support import BACKUP, PRIMARY
from test_aws_adapters import (
    TABLE, StubClient, StubMeta, StubResource, StubTable, TransactionCanceledException,
)

from er import limits
from er.aws_adapters import PAUSE_KEY, POINTER_KEY, DynamoStore
from er.config import Config
from er.fakes import ACK, NO_ANSWER, InMemoryStore, Simulation
from er.models import ACKNOWLEDGED, ACTIVE, CANCELLED, EXPIRED, Incident

NOW = 1_000_000
DAY = 86400


def incident(incident_id, created_at=NOW, **fields):
    return Incident(
        id=incident_id, sender="Robin", kind="kids", created_at=created_at,
        base_url="https://ring.example.invalid", **fields
    )


def function_timeout():
    """How long one invocation may run, in seconds, as template.yaml sets it."""
    template = os.path.join(os.path.dirname(__file__), "..", "template.yaml")
    with open(template) as handle:
        timeouts = [int(n) for n in re.findall(r"^ +Timeout: (\d+)$", handle.read(), re.M)]
    if not timeouts:
        raise AssertionError("no function Timeout found in template.yaml")
    return max(timeouts)


# ------------------------------------------------- a stricter DynamoDB stub

class Refused(Exception):
    """Stands in for DynamoDB's ValidationException. The store does not catch
    it, so a refused request fails the test that sent it."""


# Attribute names this table uses that are reserved words in DynamoDB, which
# refuses an expression that spells one out instead of using a #placeholder.
RESERVED = {"status", "ttl", "state", "result", "by"}


def as_dynamodb_returns(value):
    """Numbers come back as Decimal, inside lists too. True and False stay."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, list):
        return [as_dynamodb_returns(v) for v in value]
    if isinstance(value, dict):
        return {k: as_dynamodb_returns(v) for k, v in value.items()}
    return value


def check_storable(value):
    """boto3 will not serialise a float."""
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for inner in value:
            check_storable(inner)


def check_request(kwargs, expression_keys):
    text = " ".join(kwargs.get(key) or "" for key in expression_keys)
    for kind, pattern in (("ExpressionAttributeNames", r"#\w+"), ("ExpressionAttributeValues", r":\w+")):
        used = set(re.findall(pattern, text))
        if kind in kwargs and not kwargs[kind]:
            raise Refused("%s must not be empty" % kind)
        given = set(kwargs.get(kind) or {})
        if given != used:
            raise Refused("%s: unused %s, undefined %s" % (
                kind, sorted(given - used), sorted(used - given)))
    bare = set(re.findall(r"(?<![#:\w])[A-Za-z_]\w*", text))
    spelled_out = {word for word in bare if word.lower() in RESERVED}
    if spelled_out:
        raise Refused("reserved words used as attribute names: %s" % sorted(spelled_out))
    check_storable(kwargs.get("ExpressionAttributeValues") or {})
    check_storable(kwargs.get("Item") or {})


class StrictTable(StubTable):
    def get_item(self, Key, ConsistentRead=False):
        reply = super().get_item(Key=Key, ConsistentRead=ConsistentRead)
        if "Item" in reply:
            reply["Item"] = as_dynamodb_returns(self.items[Key["pk"]])
        return reply

    def update_item(self, **kwargs):
        check_request(kwargs, ("UpdateExpression", "ConditionExpression"))
        super().update_item(**kwargs)

    def delete_item(self, **kwargs):
        check_request(kwargs, ("ConditionExpression",))
        super().delete_item(**kwargs)


class StrictClient(StubClient):
    def transact_write_items(self, TransactItems):
        for entry in TransactItems:
            check_request(entry["Put"], ("ConditionExpression",))
        super().transact_write_items(TransactItems=TransactItems)


class StrictResource(StubResource):
    def __init__(self):
        self.items = {}
        self.table = StrictTable(self.items)
        self.meta = StubMeta(StrictClient(self.items))


class StrictStub(unittest.TestCase):
    """The checks above are only worth having if they fire."""

    def setUp(self):
        self.table = StrictResource().table

    def update(self, expression, names=None, values=None, condition=None):
        kwargs = {"Key": {"pk": "X"}, "UpdateExpression": expression}
        if names is not None:
            kwargs["ExpressionAttributeNames"] = names
        if values is not None:
            kwargs["ExpressionAttributeValues"] = values
        if condition:
            kwargs["ConditionExpression"] = condition
        self.table.update_item(**kwargs)

    def test_a_well_formed_update_goes_through_and_reads_back_as_dynamodb_would(self):
        self.update("SET #a = :a, #b = :b, #c = :c", {"#a": "n", "#b": "flag", "#c": "sent"},
                    {":a": 3, ":b": True, ":c": [1, 2]})
        item = self.table.get_item(Key={"pk": "X"})["Item"]
        self.assertEqual((type(item["n"]), item["flag"]), (Decimal, True))
        self.assertEqual([type(n) for n in item["sent"]], [Decimal, Decimal])

    def test_requests_dynamodb_would_refuse_are_refused(self):
        for refused in (
            dict(expression="SET #a = :a", names={"#a": "n", "#spare": "m"}, values={":a": 1}),
            dict(expression="SET #a = :a", names={"#a": "n"}, values={":a": 1, ":spare": 2}),
            dict(expression="SET #a = :a", names={"#a": "n"}, values={}),
            dict(expression="REMOVE #a", names={"#a": "n"}, values={}),
            dict(expression="SET #a = :a", names={}, values={":a": 1}),
            dict(expression="SET status = :a", values={":a": "active"}),
            dict(expression="SET #a = :a", names={"#a": "n"}, values={":a": 1},
                 condition="ttl < :a"),
        ):
            with self.assertRaises(Refused, msg=refused):
                self.update(**refused)
        with self.assertRaises(TypeError):
            self.update("SET #a = :a", {"#a": "n"}, {":a": [1.5]})


# ------------------------------------------------------------- the contract

class StoreContract:
    """What core relies on, whichever store is behind it. Not a TestCase by
    itself: the two classes after it supply the store."""

    def new_store(self):
        raise NotImplementedError

    def point_at(self, incident_id):
        """Leave the active-incident pointer naming this id, the way a worker
        that died between two writes can."""
        raise NotImplementedError

    def setUp(self):
        self.cfg = Config(primary_number=PRIMARY)
        self.store = self.new_store()

    def create(self, incident_id, now=NOW, **fields):
        return self.store.create_if_none_active(incident(incident_id, now, **fields), now)

    # ------------------------------------------------- create_if_none_active

    def test_a_new_incident_is_stored_and_becomes_the_active_one(self):
        created, was_created = self.create("incident-aaa")
        self.assertTrue(was_created)
        self.assertEqual(created, incident("incident-aaa"))
        self.assertEqual(self.store.get("incident-aaa"), incident("incident-aaa"))
        self.assertEqual(self.store.active_id(), "incident-aaa")

    def test_every_field_comes_back_as_it_went_in(self):
        full = dict(note="Call me now.", lat="47.610000", lon="-122.330000",
                    test=True, rehearsal=True)
        self.create("incident-aaa", **full)
        loaded = self.store.get("incident-aaa")
        self.assertEqual(loaded, incident("incident-aaa", **full))
        # Core does arithmetic on the counters and tests the flags for truth.
        for name in ("created_at", "attempts", "calls_failed", "tick_seq"):
            self.assertIs(type(getattr(loaded, name)), int, name)
        self.assertIs(loaded.test, True)
        self.assertIs(loaded.rehearsal, True)
        self.assertIs(type(loaded.lat), str)

    def test_get_of_an_unknown_id_is_none(self):
        self.assertIsNone(self.store.get("incident-zzz"))
        self.assertIsNone(self.store.active_id())

    def test_a_second_create_returns_the_active_incident_and_stores_nothing(self):
        self.create("incident-aaa", note="first")
        existing, was_created = self.create("incident-bbb", NOW + 30, note="second")
        self.assertFalse(was_created)
        self.assertEqual(existing, incident("incident-aaa", note="first"))
        self.assertIsNone(self.store.get("incident-bbb"))
        self.assertEqual(self.store.active_id(), "incident-aaa")

    def test_closing_an_incident_makes_room_for_the_next(self):
        previous = "incident-000"
        self.create(previous)
        for n, closed_as in enumerate((ACKNOWLEDGED, CANCELLED, EXPIRED), 1):
            self.assertTrue(self.store.transition(previous, ACTIVE, closed_as))
            self.assertIsNone(self.store.active_id(), closed_as)
            previous = "incident-%03d" % n
            self.assertTrue(self.create(previous, NOW + n)[1], closed_as)
            self.assertEqual(self.store.active_id(), previous)

    def test_a_pointer_left_on_a_closed_incident_does_not_block_a_new_one(self):
        # The status change went through and the release after it never ran.
        self.create("incident-aaa")
        self.store.transition("incident-aaa", ACTIVE, ACKNOWLEDGED)
        self.point_at("incident-aaa")
        created, was_created = self.create("incident-bbb", NOW + 30)
        self.assertTrue(was_created)
        self.assertEqual(created.id, "incident-bbb")
        self.assertEqual(self.store.active_id(), "incident-bbb")
        self.assertEqual(self.store.get("incident-aaa").status, ACKNOWLEDGED)

    def test_a_pointer_to_a_record_that_is_gone_does_not_block_a_new_one(self):
        self.point_at("incident-gone")
        _, was_created = self.create("incident-bbb")
        self.assertTrue(was_created)
        self.assertEqual(self.store.active_id(), "incident-bbb")

    def test_the_pointer_holds_right_up_to_its_expiry(self):
        self.create("incident-aaa")
        existing, was_created = self.create("incident-bbb", NOW + self.cfg.active_pointer_ttl)
        self.assertFalse(was_created)
        self.assertEqual(existing.id, "incident-aaa")

    def test_a_stale_pointer_does_not_block_a_new_incident(self):
        # An incident whose ticks were lost stays "active" for good. Past the
        # pointer's lifetime it must not swallow the next emergency.
        self.create("incident-aaa")
        created, was_created = self.create("incident-bbb", NOW + self.cfg.active_pointer_ttl + 1)
        self.assertTrue(was_created)
        self.assertEqual(created.id, "incident-bbb")
        self.assertEqual(self.store.active_id(), "incident-bbb")

    # ------------------------------------------------------------ transition

    def test_transition_changes_the_status_and_sets_the_fields(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.transition(
            "incident-aaa", ACTIVE, ACKNOWLEDGED, acked_via="call", acked_at=NOW + 5,
            closed_at=NOW + 5,
        ))
        self.assertEqual(
            self.store.get("incident-aaa"),
            incident("incident-aaa", status=ACKNOWLEDGED, acked_via="call",
                     acked_at=NOW + 5, closed_at=NOW + 5),
        )

    def test_only_the_first_of_two_closers_wins(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.transition("incident-aaa", ACTIVE, ACKNOWLEDGED, acked_via="call"))
        self.assertFalse(self.store.transition(
            "incident-aaa", ACTIVE, CANCELLED, closed_at=NOW + 9, cancel_reason="paused"
        ))
        # The loser changed nothing, fields included.
        self.assertEqual(
            self.store.get("incident-aaa"),
            incident("incident-aaa", status=ACKNOWLEDGED, acked_via="call"),
        )

    def test_a_failed_transition_keeps_the_incident_active(self):
        self.create("incident-aaa")
        self.assertFalse(self.store.transition("incident-aaa", CANCELLED, ACKNOWLEDGED))
        self.assertEqual(self.store.active_id(), "incident-aaa")
        self.assertFalse(self.create("incident-bbb", NOW + 30)[1])

    def test_transition_of_an_unknown_incident_creates_nothing(self):
        self.assertFalse(self.store.transition("incident-zzz", ACTIVE, CANCELLED, closed_at=NOW))
        self.assertIsNone(self.store.get("incident-zzz"))
        self.assertIsNone(self.store.active_id())

    def test_closing_an_incident_leaves_a_pointer_that_names_another_alone(self):
        self.create("incident-aaa")
        self.point_at("incident-bbb")
        self.assertTrue(self.store.transition("incident-aaa", ACTIVE, CANCELLED))
        self.assertEqual(self.store.active_id(), "incident-bbb")

    # ---------------------------------------------------------- advance_tick

    def test_advance_tick_works_once_per_sequence_number(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.advance_tick("incident-aaa", 0, 1000))
        self.assertEqual(self.store.get("incident-aaa").tick_seq, 1)
        self.assertFalse(self.store.advance_tick("incident-aaa", 0, 1000))  # the same tick, delivered twice
        self.assertFalse(self.store.advance_tick("incident-aaa", 2, 1000))  # a tick from the future
        self.assertEqual(self.store.get("incident-aaa").tick_seq, 1)
        self.assertTrue(self.store.advance_tick("incident-aaa", 1, 1000))
        self.assertEqual(self.store.get("incident-aaa").tick_seq, 2)

    def test_advance_tick_is_refused_once_closed_or_for_an_unknown_incident(self):
        self.create("incident-aaa")
        self.store.transition("incident-aaa", ACTIVE, CANCELLED)
        self.assertFalse(self.store.advance_tick("incident-aaa", 0, 1000))
        self.assertEqual(self.store.get("incident-aaa").tick_seq, 0)
        self.assertFalse(self.store.advance_tick("incident-zzz", 0, 1000))
        self.assertIsNone(self.store.get("incident-zzz"))

    # --------------------------------------------------------- claim_attempt

    def test_claim_attempt_counts_the_call_and_marks_it_in_flight(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.claim_attempt("incident-aaa", 0, NOW + 1))
        loaded = self.store.get("incident-aaa")
        self.assertEqual((loaded.attempts, loaded.call_in_flight_since), (1, NOW + 1))
        self.assertIs(type(loaded.call_in_flight_since), int)

    def test_only_one_of_two_workers_claims_an_attempt(self):
        self.create("incident-aaa")
        self.assertTrue(self.store.claim_attempt("incident-aaa", 0, NOW + 1))
        self.assertFalse(self.store.claim_attempt("incident-aaa", 0, NOW + 2))
        loaded = self.store.get("incident-aaa")
        self.assertEqual((loaded.attempts, loaded.call_in_flight_since), (1, NOW + 1))

    def test_claim_attempt_is_refused_once_closed_or_for_an_unknown_incident(self):
        self.create("incident-aaa")
        self.store.transition("incident-aaa", ACTIVE, ACKNOWLEDGED)
        self.assertFalse(self.store.claim_attempt("incident-aaa", 0, NOW))
        self.assertEqual(self.store.get("incident-aaa").attempts, 0)
        self.assertFalse(self.store.claim_attempt("incident-zzz", 0, NOW))
        self.assertIsNone(self.store.get("incident-zzz"))

    # ---------------------------------------------------------------- update

    def test_update_sets_fields_and_touches_nothing_else(self):
        self.create("incident-aaa", note="Call me now.")
        self.store.update("incident-aaa", last_call_sid="CA1", pushover_receipt="r1", calls_failed=2)
        self.assertEqual(
            self.store.get("incident-aaa"),
            incident("incident-aaa", note="Call me now.", last_call_sid="CA1",
                     pushover_receipt="r1", calls_failed=2),
        )
        self.assertEqual(self.store.active_id(), "incident-aaa")

    def test_update_with_none_removes_the_field(self):
        self.create("incident-aaa")
        self.store.claim_attempt("incident-aaa", 0, NOW)
        self.store.update("incident-aaa", call_in_flight_since=None)
        loaded = self.store.get("incident-aaa")
        self.assertIsNone(loaded.call_in_flight_since)
        self.assertEqual(loaded.attempts, 1)
        # Removing what is not there is not an error.
        self.store.update("incident-aaa", call_in_flight_since=None)
        self.assertIsNone(self.store.get("incident-aaa").call_in_flight_since)

    def test_update_can_set_and_remove_in_one_go(self):
        # What core does when a call cannot be placed.
        self.create("incident-aaa")
        self.store.claim_attempt("incident-aaa", 0, NOW)
        self.store.update("incident-aaa", call_in_flight_since=None, calls_failed=1)
        loaded = self.store.get("incident-aaa")
        self.assertEqual((loaded.call_in_flight_since, loaded.calls_failed, loaded.attempts), (None, 1, 1))

    # ----------------------------------------------------------------- pause

    def test_pause_round_trip(self):
        self.assertEqual(self.store.pause_state(), (0, ""))
        self.store.set_pause(NOW + 600, "Robin")
        until, by = self.store.pause_state()
        self.assertEqual((until, by), (NOW + 600, "Robin"))
        self.assertIs(type(until), int)
        self.store.set_pause(NOW + 900, "Assistant")  # a later pause replaces it
        self.assertEqual(self.store.pause_state(), (NOW + 900, "Assistant"))
        self.store.set_pause(0)
        self.assertEqual(self.store.pause_state(), (0, ""))

    def test_pause_without_a_name_and_lifting_a_pause_that_is_not_set(self):
        self.store.set_pause(0)
        self.assertEqual(self.store.pause_state(), (0, ""))
        self.store.set_pause(NOW + 600)
        self.assertEqual(self.store.pause_state(), (NOW + 600, ""))

    def test_a_pause_does_not_touch_the_active_incident(self):
        # Stopping the incident is core's job; the store only keeps the flag.
        self.create("incident-aaa")
        self.store.set_pause(NOW + 600, "Robin")
        self.assertEqual(self.store.active_id(), "incident-aaa")
        self.assertEqual(self.store.get("incident-aaa").status, ACTIVE)

    # ----------------------------------------------------- break-glass slots

    def test_one_claim_per_interval(self):
        self.assertEqual(self.store.claim_slot("bg:Assistant", NOW, 900, 8), (True, 0, ""))
        # Two requests in the same second: one alert.
        self.assertEqual(self.store.claim_slot("bg:Assistant", NOW, 900, 8), (False, 900, "interval"))
        self.assertEqual(self.store.claim_slot("bg:Assistant", NOW + 300, 900, 8), (False, 600, "interval"))
        self.assertEqual(self.store.claim_slot("bg:Assistant", NOW + 899, 900, 8), (False, 1, "interval"))
        self.assertEqual(self.store.claim_slot("bg:Assistant", NOW + 900, 900, 8), (True, 0, ""))

    def test_a_refused_claim_takes_nothing(self):
        self.store.claim_slot("k", NOW, 900, 8)
        self.store.claim_slot("k", NOW + 300, 900, 8)
        self.assertEqual(self.store.peek_slot("k", NOW + 300, 8), (600, "interval", 1))

    def test_each_key_has_its_own_slot(self):
        self.assertTrue(self.store.claim_slot("bg:Assistant", NOW, 900, 8)[0])
        self.assertTrue(self.store.claim_slot("bg-test:Assistant", NOW, 900, 8)[0])
        self.assertEqual(self.store.peek_slot("bg:Other", NOW, 8), (0, "", 0))

    def test_the_daily_cap_is_a_rolling_24_hours(self):
        for i in range(3):
            self.assertTrue(self.store.claim_slot("k", NOW + i * 1000, 900, 3)[0])
        self.assertEqual(self.store.claim_slot("k", NOW + 3000, 900, 3), (False, DAY - 3000, "daily_cap"))
        self.assertEqual(self.store.claim_slot("k", NOW + DAY - 1, 900, 3), (False, 1, "daily_cap"))
        # The oldest alert is now a day old and no longer counts.
        self.assertEqual(self.store.claim_slot("k", NOW + DAY, 900, 3), (True, 0, ""))
        self.assertEqual(self.store.peek_slot("k", NOW + DAY + 900, 3), (100, "daily_cap", 3))

    def test_a_cap_of_zero_is_no_cap_and_an_interval_of_zero_is_no_interval(self):
        for i in range(12):
            self.assertTrue(self.store.claim_slot("uncapped", NOW + i * 900, 900, 0)[0], i)
        self.assertEqual(self.store.claim_slot("eager", NOW, 0, 2), (True, 0, ""))
        self.assertEqual(self.store.claim_slot("eager", NOW, 0, 2), (True, 0, ""))
        self.assertEqual(self.store.claim_slot("eager", NOW, 0, 2), (False, DAY, "daily_cap"))

    def test_the_interval_is_reported_when_both_limits_refuse(self):
        self.store.claim_slot("k", NOW, 900, 1)
        self.assertEqual(self.store.claim_slot("k", NOW + 10, 900, 1), (False, 890, "interval"))
        self.assertEqual(self.store.peek_slot("k", NOW + 10, 1), (890, "interval", 1))

    def test_peek_says_what_a_claim_would_and_takes_nothing(self):
        self.assertEqual(self.store.peek_slot("k", NOW, 2), (0, "", 0))
        self.assertEqual(self.store.peek_slot("k", NOW, 2), (0, "", 0))
        self.store.claim_slot("k", NOW, 900, 2)
        self.assertEqual(self.store.peek_slot("k", NOW + 100, 2), (800, "interval", 1))
        self.assertEqual(self.store.peek_slot("k", NOW + 900, 2), (0, "", 1))
        self.store.claim_slot("k", NOW + 900, 900, 2)
        self.assertEqual(self.store.peek_slot("k", NOW + 1800, 2), (DAY - 1800, "daily_cap", 2))

    def test_release_gives_the_slot_back(self):
        # The alert failed to send, so the caller may try again at once.
        self.store.claim_slot("k", NOW, 900, 8)
        self.store.release_slot("k", NOW)
        self.assertEqual(self.store.peek_slot("k", NOW + 1, 8), (0, "", 0))
        self.assertEqual(self.store.claim_slot("k", NOW + 1, 900, 8), (True, 0, ""))

    def test_release_keeps_the_count_of_alerts_that_did_go_out(self):
        self.store.claim_slot("k", NOW, 900, 8)
        self.store.claim_slot("k", NOW + 900, 900, 8)
        self.store.release_slot("k", NOW + 900)
        self.assertEqual(self.store.peek_slot("k", NOW + 901, 8), (0, "", 1))

    def test_release_of_a_slot_that_was_never_taken_changes_nothing(self):
        self.store.release_slot("nobody", NOW)
        self.assertEqual(self.store.peek_slot("nobody", NOW, 8), (0, "", 0))
        self.store.claim_slot("k", NOW, 900, 8)
        self.store.release_slot("k", NOW + 5)
        self.assertEqual(self.store.peek_slot("k", NOW + 10, 8), (890, "interval", 1))

    # ----------------------------------------------------- idempotency keys

    def test_idempotency_key_lifecycle(self):
        result = {"delivered": True, "retry_after_seconds": 900}
        self.assertIsNone(self.store.idem_begin("bg:Assistant:evt-1", NOW))
        self.assertEqual(self.store.idem_begin("bg:Assistant:evt-1", NOW + 1), {"state": "pending"})
        self.store.idem_finish("bg:Assistant:evt-1", result)
        prior = self.store.idem_begin("bg:Assistant:evt-1", NOW + 2)
        self.assertEqual(prior, {"state": "done", "result": result})
        self.assertIs(prior["result"]["delivered"], True)
        self.assertIs(type(prior["result"]["retry_after_seconds"]), int)

    def test_each_idempotency_key_stands_alone(self):
        self.assertIsNone(self.store.idem_begin("bg:Assistant:evt-1", NOW))
        self.assertIsNone(self.store.idem_begin("bg:Assistant:evt-2", NOW))
        self.store.idem_finish("bg:Assistant:evt-1", {"delivered": True})
        self.assertEqual(self.store.idem_begin("bg:Assistant:evt-2", NOW + 1), {"state": "pending"})

    def test_forgetting_a_key_lets_it_be_used_again(self):
        # A refused or failed request is not remembered.
        self.store.idem_begin("k", NOW)
        self.store.idem_finish("k", None)
        self.assertIsNone(self.store.idem_begin("k", NOW + 1))
        self.store.idem_finish("never-begun", None)  # nothing to forget is not an error

    def test_a_pending_record_from_a_dead_invocation_can_be_taken_over(self):
        window = limits.IDEM_PENDING_SECONDS
        self.assertIsNone(self.store.idem_begin("k", NOW))
        # Still inside the window: the first request may be alive.
        self.assertEqual(self.store.idem_begin("k", NOW + window), {"state": "pending"})
        # Past it: whoever began this never finished.
        self.assertIsNone(self.store.idem_begin("k", NOW + window + 1))
        # The key now belongs to the new request, with a window of its own.
        self.assertEqual(self.store.idem_begin("k", NOW + window + 2), {"state": "pending"})
        self.assertEqual(self.store.idem_begin("k", NOW + 2 * window + 1), {"state": "pending"})
        self.assertIsNone(self.store.idem_begin("k", NOW + 2 * window + 2))

    def test_a_finished_record_is_never_taken_over(self):
        # Otherwise a retry long after a delivered alert would send it again.
        self.store.idem_begin("k", NOW)
        self.store.idem_finish("k", {"delivered": True})
        for later in (limits.IDEM_PENDING_SECONDS + 1, DAY, 30 * DAY):
            self.assertEqual(
                self.store.idem_begin("k", NOW + later),
                {"state": "done", "result": {"delivered": True}}, later,
            )


class InMemoryStoreContract(StoreContract, unittest.TestCase):
    def new_store(self):
        return InMemoryStore()

    def point_at(self, incident_id):
        self.store.active_id_ = incident_id



class DynamoStoreContract(StoreContract, unittest.TestCase):
    def new_store(self):
        self.resource = StrictResource()
        return DynamoStore(TABLE, self.cfg, resource=self.resource)

    def point_at(self, incident_id):
        self.resource.items[POINTER_KEY] = {
            "pk": POINTER_KEY, "incident_id": incident_id,
            "expires_at": NOW + self.cfg.active_pointer_ttl,
        }


# --------------------------------------------- what only DynamoStore can do

class DynamoStoreRaces(unittest.TestCase):
    """Losing a race to another worker, and the bookkeeping DynamoDB needs.
    None of it can happen to the in-memory store."""

    def setUp(self):
        self.cfg = Config(primary_number=PRIMARY)
        self.resource = StrictResource()
        self.items = self.resource.items
        self.table = self.resource.table
        self.client = self.resource.meta.client
        self.store = DynamoStore(TABLE, self.cfg, resource=self.resource)

    def cancel_transactions(self, times):
        """Make the next `times` transactions fail as if the pointer had been
        taken, then let the rest through. Returns the list of attempts."""
        attempts = []
        real = self.client.transact_write_items

        def transact(TransactItems):
            attempts.append(TransactItems)
            if len(attempts) <= times:
                raise TransactionCanceledException()
            real(TransactItems=TransactItems)

        self.client.transact_write_items = transact
        return attempts

    def test_create_tries_again_when_the_pointer_was_freed_a_moment_ago(self):
        attempts = self.cancel_transactions(1)
        _, was_created = self.store.create_if_none_active(incident("incident-aaa"), NOW)
        self.assertTrue(was_created)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.store.active_id(), "incident-aaa")

    def test_create_raises_when_the_pointer_cannot_be_claimed(self):
        # Raising is what lets the sender's Shortcut fall back; returning
        # quietly would look like an alert that went out.
        attempts = self.cancel_transactions(99)
        with self.assertRaises(RuntimeError) as raised:
            self.store.create_if_none_active(incident("incident-aaa"), NOW)
        self.assertIn("active-incident pointer", str(raised.exception))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.items, {})

    def someone_else_writes_after_every_read(self):
        reads = []
        real = self.table.get_item

        def get_item(Key, ConsistentRead=False):
            reply = real(Key=Key, ConsistentRead=ConsistentRead)
            reads.append(Key["pk"])
            self.items[Key["pk"]]["ver"] += 1
            return reply

        self.table.get_item = get_item
        return reads

    def test_claim_slot_stands_down_after_losing_the_race_three_times(self):
        self.store.claim_slot("k", NOW, 900, 8)
        reads = self.someone_else_writes_after_every_read()
        self.assertEqual(self.store.claim_slot("k", NOW + 1000, 900, 8), (False, 900, "interval"))
        self.assertEqual(reads, ["COOLDOWN#k"] * 3)
        # Nothing of ours was written.
        self.assertEqual(self.items["COOLDOWN#k"]["sent"], [NOW])
        self.assertEqual(self.items["COOLDOWN#k"]["free_at"], NOW + 900)

    def test_standing_down_always_asks_the_caller_to_wait(self):
        # With no interval configured, a wait of 0 would read as "send now".
        self.store.claim_slot("k", NOW, 0, 8)
        self.someone_else_writes_after_every_read()
        self.assertEqual(self.store.claim_slot("k", NOW + 1, 0, 8), (False, 1, "interval"))

    def test_every_read_is_strongly_consistent(self):
        # An eventually consistent read can miss the write just before it:
        # a pause that was just set, or an acknowledgment.
        self.store.create_if_none_active(incident("incident-aaa"), NOW)
        self.store.get("incident-aaa")
        self.store.active_id()
        self.store.pause_state()
        self.store.claim_slot("k", NOW, 900, 8)
        self.store.peek_slot("k", NOW, 8)
        self.store.release_slot("k", NOW)
        self.store.idem_begin("k", NOW)
        self.store.idem_begin("k", NOW + 1)
        reads = [details for name, details in self.table.calls if name == "get_item"]
        self.assertEqual(
            {details["Key"]["pk"] for details in reads},
            {"INC#incident-aaa", POINTER_KEY, PAUSE_KEY, "COOLDOWN#k", "IDEM#k"},
        )
        for details in reads:
            self.assertIs(details["ConsistentRead"], True, details["Key"])

    def test_records_expire_and_the_pause_leaves_no_record_behind(self):
        # DynamoDB deletes an item some time after the moment in its "ttl".
        self.store.create_if_none_active(incident("incident-aaa"), NOW)
        self.store.set_pause(NOW + 600, "Robin")
        self.store.claim_slot("k", NOW, 900, 8)
        self.store.idem_begin("k", NOW)
        self.assertEqual(self.items["INC#incident-aaa"]["ttl"], NOW + self.cfg.record_ttl)
        self.assertEqual(self.items[PAUSE_KEY]["ttl"], NOW + 600 + DAY)
        # A slot has to outlive the 24 hours its count looks back over.
        self.assertGreater(self.items["COOLDOWN#k"]["ttl"], NOW + DAY)
        self.assertEqual(self.items["IDEM#k"]["ttl"], NOW + DAY)
        self.store.set_pause(0)
        self.assertNotIn(PAUSE_KEY, self.items)

    def test_the_pending_window_outlasts_one_invocation(self):
        # A pending record is taken over once it is older than the window. If
        # an invocation could still be running by then, two live requests
        # with one key would both send.
        self.assertGreater(limits.IDEM_PENDING_SECONDS, function_timeout())


# ------------------------------------- the whole service on either store

def story_on(store, play, **settings):
    """Run one scenario in virtual time on the given store. Returns the log
    and the incidents it started, as the store holds them at the end."""
    config = Config(**dict(dict(primary_number=PRIMARY, primary_attempts=2), **settings))
    sim = Simulation(config, lambda n, to: NO_ANSWER)
    sim.store = sim.service.store = store
    # Ids are random; two runs can only be compared if they use the same ones.
    ids = ("incident-%03d" % n for n in itertools.count(1))
    with mock.patch("secrets.token_urlsafe", lambda nbytes=None: next(ids)):
        incidents = play(sim) or []
        sim.run()
    return sim.log, [store.get(started.id) for started in incidents]


class ServiceOnEitherStore(unittest.TestCase):
    """A few whole incidents, played once on each store. The stores differ in
    everything but behaviour, so the two runs must tell the same story."""

    def both(self, play, **settings):
        cfg = Config(primary_number=PRIMARY)
        in_memory = story_on(InMemoryStore(), play, **settings)
        dynamo = story_on(DynamoStore(TABLE, cfg, resource=StrictResource()), play, **settings)
        self.assertEqual(dynamo, in_memory)
        return dynamo

    def test_push_then_calls_that_ring_out_until_it_expires(self):
        log, (record,) = self.both(lambda sim: [sim.trigger(note="Call me now.")],
                                   backup_number=BACKUP, backup_attempts=1)
        self.assertEqual((record.status, record.attempts, record.calls_failed), (EXPIRED, 3, 0))
        self.assertEqual(sum("placed to" in line for line in log), 3)

    def test_acknowledged_on_the_second_call(self):
        def play(sim):
            sim.behaviour = lambda n, to: ACK if n == 2 else NO_ANSWER
            return [sim.trigger()]

        _, (record,) = self.both(play)
        self.assertEqual((record.status, record.acked_via, record.attempts), (ACKNOWLEDGED, "call", 2))

    def test_push_only_and_call_only(self):
        _, (record,) = self.both(lambda sim: [sim.trigger()], call_enabled=False)
        self.assertEqual((record.status, record.attempts), (EXPIRED, 0))
        _, (record,) = self.both(lambda sim: [sim.trigger()], push_enabled=False)
        self.assertEqual((record.status, record.attempts, record.pushover_receipt), (EXPIRED, 2, None))

    def test_a_drill_is_replaced_by_a_real_alert_and_a_pause_stops_that(self):
        def play(sim):
            drill = sim.trigger(test=True)
            real = sim.trigger()
            sim.clock.t += 40
            sim.service.pause("Robin", 600)
            return [drill, real]

        with self.assertLogs("er", level="WARNING"):  # both are worth a warning
            _, (drill, real) = self.both(play)
        self.assertEqual((drill.status, drill.cancel_reason), (CANCELLED, "superseded"))
        self.assertEqual((real.status, real.cancel_reason), (CANCELLED, "paused"))

    def test_break_glass_limits_and_idempotency(self):
        def play(sim):
            send = sim.service.break_glass
            sim.say(repr(send("Assistant", "Call the school.", idempotency_key="evt-1")))
            sim.say(repr(send("Assistant", "Call the school.", idempotency_key="evt-1")))
            sim.say(repr(send("Assistant", "Something else.", idempotency_key="evt-2")))
            sim.clock.t += 900
            sim.say(repr(send("Assistant", "Something else.", idempotency_key="evt-2")))
            sim.say(repr(send("Assistant", "A test.", test=True)))
            sim.say(repr(sim.service.break_glass_status("Assistant")))
            sim.clock.t += 900
            sim.say(repr(send("Assistant", "One too many.")))

        log, _ = self.both(play, break_glass_daily_cap=2)
        self.assertIn("'duplicate': True", log[1])
        self.assertIn("'reason': 'interval'", log[2])
        self.assertIn("'delivered': True", log[3])
        self.assertIn("'sent_last_24h': 2", log[5])
        self.assertIn("'reason': 'daily_cap'", log[6])


if __name__ == "__main__":
    unittest.main()
