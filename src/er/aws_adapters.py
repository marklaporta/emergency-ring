"""DynamoDB incident store and SQS tick scheduler.

One table, string key "pk":
  INC#<id>        the incident record
  ACTIVE          a pointer to the single active incident, used to de-duplicate triggers
  PAUSE           the kill switch: when the pause ends and who set it
  COOLDOWN#<key>  one caller's break-glass or self-test window and daily count
  IDEM#<key>      the outcome of a break-glass request sent with an idempotency key
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import limits
from .config import MAX_DELAY, Config
from .models import ACTIVE, Incident

POINTER_KEY = "ACTIVE"
PAUSE_KEY = "PAUSE"


def client_config() -> Any:
    """Fail fast. The defaults wait 60 seconds and retry up to ten times,
    far past the function's own timeout, so a slow AWS call would end in a
    killed invocation with no error for the caller to fall back on."""
    from botocore.config import Config as BotoConfig  # provided by the Lambda runtime

    return BotoConfig(
        connect_timeout=2, read_timeout=4, retries={"mode": "standard", "total_max_attempts": 3}
    )


def _key(incident_id: str) -> Dict[str, str]:
    return {"pk": "INC#" + incident_id}


class DynamoStore:
    def __init__(self, table_name: str, config: Config, resource: Any = None) -> None:
        if resource is None:
            import boto3  # provided by the Lambda runtime

            resource = boto3.resource("dynamodb", config=client_config())
        self.table = resource.Table(table_name)
        self.table_name = table_name
        self.client = resource.meta.client  # accepts plain Python types
        self.cfg = config

    def create_if_none_active(self, incident: Incident, now: int) -> Tuple[Incident, bool]:
        item = dict(incident.to_dict(), ttl=now + self.cfg.record_ttl, **_key(incident.id))
        pointer = {
            "pk": POINTER_KEY,
            "incident_id": incident.id,
            "expires_at": now + self.cfg.pointer_ttl,
        }
        transaction = [
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": pointer,
                    # A stale pointer must never block a new emergency.
                    "ConditionExpression": "attribute_not_exists(pk) OR expires_at < :now",
                    "ExpressionAttributeValues": {":now": now},
                }
            },
            {"Put": {"TableName": self.table_name, "Item": item}},
        ]
        for _ in range(2):
            try:
                self.client.transact_write_items(TransactItems=transaction)
                return incident, True
            except self.client.exceptions.TransactionCanceledException:
                held = self.active_id()
                existing = self.get(held) if held else None
                if existing is not None and existing.status == ACTIVE:
                    return existing, False
                # The pointer is held by an incident that is closed or gone (its
                # release never ran), or was released a moment ago. Neither may
                # swallow a new emergency: clear it and try once more.
                if held:
                    self._release_pointer(held)
        raise RuntimeError("could not claim the active-incident pointer")

    def get(self, incident_id: str) -> Optional[Incident]:
        item = self.table.get_item(Key=_key(incident_id), ConsistentRead=True).get("Item")
        return Incident.from_dict(item) if item else None

    def active_id(self) -> Optional[str]:
        held = self.table.get_item(Key={"pk": POINTER_KEY}, ConsistentRead=True).get("Item")
        return held["incident_id"] if held else None

    def _write(
        self, key: Dict[str, str], fields: Dict[str, Any], condition: str = "",
        values: Optional[Dict[str, Any]] = None, names: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Set every field on one item (a value of None removes the field),
        if the condition holds. False when it does not."""
        names, values = dict(names or {}), dict(values or {})
        sets, removes = [], []
        for i, (field, value) in enumerate(fields.items()):
            names["#f%d" % i] = field
            if value is None:
                removes.append("#f%d" % i)
            else:
                values[":v%d" % i] = value
                sets.append("#f%d = :v%d" % (i, i))
        parts = []
        if sets:
            parts.append("SET " + ", ".join(sets))
        if removes:
            parts.append("REMOVE " + ", ".join(removes))
        kwargs: Dict[str, Any] = {
            "Key": key, "UpdateExpression": " ".join(parts), "ExpressionAttributeNames": names,
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        if condition:
            kwargs["ConditionExpression"] = condition
        try:
            self.table.update_item(**kwargs)
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def _write_if(self, incident_id: str, fields: Dict[str, Any], condition: str,
                  values: Dict[str, Any]) -> bool:
        """A change to an incident that only one of two racing workers may make."""
        return self._write(_key(incident_id), fields, condition, values, {"#status": "status"})

    def transition(self, incident_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        sets = dict(fields, status=to_status)
        ok = self._write_if(incident_id, sets, "#status = :from", {":from": from_status})
        if ok and to_status != ACTIVE:
            self._release_pointer(incident_id)
        return ok

    def _release_pointer(self, incident_id: str) -> None:
        try:
            self.table.delete_item(
                Key={"pk": POINTER_KEY},
                ConditionExpression="incident_id = :id",
                ExpressionAttributeValues={":id": incident_id},
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            pass

    def advance_tick(self, incident_id: str, expected_seq: int, now: int) -> bool:
        return self._write_if(
            incident_id,
            {"tick_seq": expected_seq + 1, "tick_at": now},
            "#status = :active AND tick_seq = :seq",
            {":active": ACTIVE, ":seq": expected_seq},
        )

    def claim_attempt(self, incident_id: str, expected_attempts: int, now: int) -> bool:
        return self._write_if(
            incident_id,
            {"attempts": expected_attempts + 1, "call_in_flight_since": now},
            "#status = :active AND attempts = :n",
            {":active": ACTIVE, ":n": expected_attempts},
        )

    # ---- pause: one item, read on every trigger, break-glass and tick

    def set_pause(self, until: int, by: str = "") -> None:
        if until <= 0:
            self.table.delete_item(Key={"pk": PAUSE_KEY})
            return
        self._write({"pk": PAUSE_KEY}, {"until": until, "ttl": until + limits.DAY, "by": by})

    def pause_state(self) -> Tuple[int, str]:
        item = self.table.get_item(Key={"pk": PAUSE_KEY}, ConsistentRead=True).get("Item") or {}
        return int(item.get("until", 0)), str(item.get("by", ""))

    # ---- break-glass limits: one item per key, guarded by a version number

    def _slot(self, key: str) -> Tuple[Dict[str, str], Dict[str, Any]]:
        pk = {"pk": "COOLDOWN#" + key}
        item = self.table.get_item(Key=pk, ConsistentRead=True).get("Item") or {}
        return pk, item

    def _write_slot(self, pk: Dict[str, str], item: Dict[str, Any], free_at: int,
                    sent: List[int], now: int) -> bool:
        version = int(item.get("ver", 0))
        return self._write(
            pk,
            {"free_at": free_at, "sent": sent, "ver": version + 1, "ttl": now + 2 * limits.DAY},
            # Only if nobody else changed it since we read it.
            "attribute_not_exists(pk) OR ver = :ver",
            {":ver": version},
        )

    def claim_slot(self, key: str, now: int, interval: int, daily_cap: int) -> Tuple[bool, int, str]:
        for _ in range(3):
            pk, item = self._slot(key)
            sent = [int(t) for t in item.get("sent", [])]
            ok, wait, reason, recent = limits.decide(int(item.get("free_at", 0)), sent, now, daily_cap)
            if not ok:
                return False, wait, reason
            if self._write_slot(pk, item, now + interval, recent + [now], now):
                return True, 0, ""
        # Lost the race three times: someone else is sending right now.
        return False, max(interval, 1), limits.INTERVAL

    def peek_slot(self, key: str, now: int, daily_cap: int) -> Tuple[int, str, int]:
        _, item = self._slot(key)
        sent = [int(t) for t in item.get("sent", [])]
        _, wait, reason, recent = limits.decide(int(item.get("free_at", 0)), sent, now, daily_cap)
        return wait, reason, len(recent)

    def release_slot(self, key: str, at: int) -> None:
        pk, item = self._slot(key)
        sent = [int(t) for t in item.get("sent", [])]
        if at in sent:
            sent.remove(at)
            self._write_slot(pk, item, 0, sent, at)

    def idem_begin(self, key: str, now: int) -> Optional[dict]:
        pk = {"pk": "IDEM#" + key}
        fresh = self._write(
            pk,
            {"state": "pending", "began": now, "ttl": now + limits.DAY},
            "attribute_not_exists(pk) OR #began < :stale",
            {":stale": now - limits.IDEM_PENDING_SECONDS},
            {"#began": "began"},
        )
        if fresh:
            return None
        item = self.table.get_item(Key=pk, ConsistentRead=True).get("Item") or {}
        if item.get("state") == "done":
            return {"state": "done", "result": json.loads(item.get("result") or "{}")}
        return {"state": "pending"}

    def idem_finish(self, key: str, result: Optional[dict]) -> None:
        pk = {"pk": "IDEM#" + key}
        if result is None:
            self.table.delete_item(Key=pk)
            return
        # "began" goes, so a finished record is never mistaken for an abandoned one.
        self._write(pk, {"state": "done", "result": json.dumps(result), "began": None})

    def update(self, incident_id: str, **fields: Any) -> None:
        # Only an existing record: update_item would otherwise create a
        # half-empty one that cannot be read back.
        self._write(_key(incident_id), fields, "attribute_exists(pk)")


class SqsScheduler:
    """A delayed SQS message is the timer. SQS redelivers if the tick fails."""

    def __init__(self, queue_url: str, client: Any = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("sqs", config=client_config())
        self.client = client
        self.queue_url = queue_url

    def _send(self, body: Dict[str, Any], delay_seconds: int) -> None:
        self.client.send_message(
            QueueUrl=self.queue_url,
            DelaySeconds=min(max(int(delay_seconds), 0), MAX_DELAY),
            MessageBody=json.dumps(body),
        )

    def schedule_tick(self, incident_id: str, seq: int, delay_seconds: int) -> None:
        self._send({"incident_id": incident_id, "seq": seq}, delay_seconds)

    def schedule_push(self, incident_id: str, delay_seconds: int) -> None:
        self._send({"incident_id": incident_id, "action": "push"}, delay_seconds)
