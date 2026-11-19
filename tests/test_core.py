import inspect
import unittest

from support import BACKUP, PRIMARY

from er import aws_adapters, core, fakes, ports, pushover_adapter, twilio_adapter
from er.config import Config
from er.fakes import ACK, LOST_CALLBACK, NO_ANSWER, VOICEMAIL, Simulation
from er.models import ACKNOWLEDGED, ACTIVE, CANCELLED, EXPIRED


def make(behaviour, backup=BACKUP, **overrides):
    config = Config(
        primary_number=PRIMARY,
        backup_number=backup,
        primary_attempts=3,
        backup_attempts=2,
        **dict({"arm_offset_seconds": 0, "cycle_seconds": 60}, **overrides)
    )
    return Simulation(config, behaviour)


class RedialLoop(unittest.TestCase):
    def test_ack_on_first_call_stops_everything(self):
        sim = make(lambda n, to: ACK)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.store.get(incident.id).acked_via, "call")

    def test_redials_until_acknowledged(self):
        sim = make(lambda n, to: ACK if n == 3 else NO_ANSWER)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)
        self.assertEqual([c["to"] for c in sim.telephony.calls], [PRIMARY] * 3)

    def test_voicemail_does_not_count_as_acknowledgment(self):
        sim = make(lambda n, to: VOICEMAIL if n == 1 else ACK)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 2)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)

    def test_escalates_to_backup_then_expires(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(
            [c["to"] for c in sim.telephony.calls], [PRIMARY] * 3 + [BACKUP] * 2
        )
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)
        self.assertIn("Nobody acknowledged", sim.notifier.sender_messages[-1][1])

    def test_no_backup_configured(self):
        sim = make(lambda n, to: NO_ANSWER, backup=None)
        sim.trigger()
        sim.run()
        self.assertEqual([c["to"] for c in sim.telephony.calls], [PRIMARY] * 3)

    def test_backup_hears_that_owner_did_not_respond(self):
        sim = make(lambda n, to: ACK if to == BACKUP else NO_ANSWER)
        sim.trigger()
        sim.run()
        self.assertTrue(any("The owner has not responded" in line for line in sim.log))

    def test_lost_status_callback_delays_but_does_not_stop_redial(self):
        sim = make(lambda n, to: LOST_CALLBACK if n == 1 else ACK)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 2)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)

    def test_carrier_failure_on_first_call_is_retried(self):
        sim = make(lambda n, to: ACK)
        sim.telephony.fail_next = True
        incident = sim.trigger()
        self.assertEqual(len(sim.telephony.calls), 0)
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)


class Deduplication(unittest.TestCase):
    def test_second_trigger_attaches_to_active_incident(self):
        sim = make(lambda n, to: NO_ANSWER)
        first = sim.trigger()
        second = sim.trigger()
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(len(sim.notifier.emergencies), 1)

    def test_new_incident_allowed_after_previous_closed(self):
        sim = make(lambda n, to: ACK)
        first = sim.trigger()
        sim.run()
        second = sim.trigger()
        self.assertNotEqual(first.id, second.id)

    def test_duplicate_tick_delivery_places_one_call(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        sim.service.call_status(incident.id, "CA0001", "no-answer")
        sim.clock.t += 60
        self.assertEqual(sim.service.tick(incident.id, 0), "dialed")
        self.assertEqual(sim.service.tick(incident.id, 0), "stale")
        self.assertEqual(len(sim.telephony.calls), 2)

    def test_duplicate_chain_does_not_double_the_dial_rate(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        # Deliver the first tick message twice, as SQS is allowed to.
        due, n, incident_id, seq = sim.scheduler.queue[0]
        sim.scheduler.schedule_tick(incident_id, seq, 60)
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 5)
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)


class Closing(unittest.TestCase):
    def test_pushover_ack_stops_calls(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        receipt = sim.store.get(incident.id).pushover_receipt
        sim.notifier.acknowledged.add(receipt)
        self.assertFalse(sim.service.acknowledge_from_push(incident.id, "wrong"))
        self.assertTrue(sim.service.acknowledge_from_push(incident.id, receipt))
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.store.get(incident.id).acked_via, "pushover")

    def test_push_callback_with_right_receipt_is_ignored_unless_pushover_confirms(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        receipt = sim.store.get(incident.id).pushover_receipt
        # A forged callback that somehow knows the receipt id.
        self.assertFalse(sim.service.acknowledge_from_push(incident.id, receipt))
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)
        self.assertEqual(sim.notifier.lookups, 1)

    def test_push_callback_raises_when_pushover_cannot_be_asked(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        receipt = sim.store.get(incident.id).pushover_receipt
        sim.notifier.lookup_fails = True
        with self.assertRaises(RuntimeError):
            sim.service.acknowledge_from_push(incident.id, receipt)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)

    def test_tick_notices_push_ack_when_callback_never_arrives(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        sim.notifier.acknowledged.add(sim.store.get(incident.id).pushover_receipt)
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)
        self.assertEqual(sim.store.get(incident.id).acked_via, "pushover")

    def test_failed_receipt_lookup_on_tick_keeps_calling(self):
        sim = make(lambda n, to: ACK if n == 2 else NO_ANSWER)
        incident = sim.trigger()
        sim.notifier.lookup_fails = True
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 2)
        self.assertEqual(sim.store.get(incident.id).acked_via, "call")

    def test_ack_retracts_push_and_tells_sender(self):
        sim = make(lambda n, to: ACK)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.notifier.cancelled, ["rcpt-%s" % incident.id])
        self.assertEqual(sim.notifier.sender_messages, [("Robin", "The owner acknowledged your alert.")])

    def test_cancel_stops_calls_and_hangs_up(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        self.assertFalse(sim.service.cancel(incident.id, "Someone else"))
        self.assertTrue(sim.service.cancel(incident.id, "Robin"))
        sim.run()
        self.assertEqual(sim.store.get(incident.id).status, CANCELLED)
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.telephony.hung_up, ["CA0001"])

    def test_only_digit_1_acknowledges(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        twiml = sim.service.ack_from_call(incident.id, "5")
        self.assertIn("<Gather", twiml)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)

    def test_answering_after_close_says_already_handled(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        sim.service.acknowledge(incident.id, via="call")
        self.assertIn("already been handled", sim.service.voice_twiml(incident.id))

    def test_spoken_text_is_xml_escaped(self):
        sim = make(lambda n, to: NO_ANSWER)
        incident = sim.trigger(note="fire & <smoke>")
        twiml = sim.service.voice_twiml(incident.id)
        self.assertIn("fire &amp; &lt;smoke&gt;", twiml)


class Ports(unittest.TestCase):
    """No type checker runs here, so nothing else would notice a fake or an
    adapter drifting away from the interface the core calls."""

    IMPLEMENTATIONS = {
        ports.Store: (fakes.InMemoryStore, aws_adapters.DynamoStore),
        ports.Telephony: (
            fakes.FakeTelephony, twilio_adapter.TwilioTelephony, twilio_adapter.NullTelephony,
        ),
        ports.Notifier: (
            fakes.FakeNotifier, pushover_adapter.PushoverNotifier, pushover_adapter.NullNotifier,
        ),
        ports.Scheduler: (fakes.FakeScheduler, aws_adapters.SqsScheduler),
        ports.Clock: (fakes.FakeClock, core.SystemClock),
    }

    @staticmethod
    def parameters(function):
        return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]

    def test_every_port_is_listed_here(self):
        declared = {member for _, member in inspect.getmembers(ports, inspect.isclass)
                    if member.__module__ == ports.__name__}
        self.assertEqual(declared, set(self.IMPLEMENTATIONS))

    def test_fakes_and_adapters_have_every_method_with_the_same_parameters(self):
        for port, implementations in self.IMPLEMENTATIONS.items():
            methods = {name: member for name, member in vars(port).items()
                       if inspect.isfunction(member) and not name.startswith("_")}
            self.assertTrue(methods, port.__name__)
            for implementation in implementations:
                for name, declared in methods.items():
                    where = "%s.%s" % (implementation.__name__, name)
                    self.assertTrue(hasattr(implementation, name), where + " is missing")
                    self.assertEqual(self.parameters(getattr(implementation, name)),
                                     self.parameters(declared), where)


if __name__ == "__main__":
    unittest.main()
