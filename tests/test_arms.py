"""The two alert arms: which are on, which goes first, and how they stop each other."""
import json
import os
import re
import unittest

from support import BACKUP, PRIMARY

from er import handler
from er.config import Config
from er.fakes import ACK, NO_ANSWER, Simulation
from er.models import ACKNOWLEDGED, ACTIVE, EXPIRED


def make(behaviour=lambda n, to: NO_ANSWER, **overrides):
    settings = dict(primary_number=PRIMARY, primary_attempts=3)
    settings.update(overrides)
    return Simulation(Config(**settings), behaviour)


def times(sim, needle):
    """Seconds after the trigger of every log line containing needle."""
    return [int(line.split("s")[0].replace("t+", "")) for line in sim.log if needle in line]


class Defaults(unittest.TestCase):
    def test_defaults_are_push_first_30_seconds_then_every_90(self):
        config = Config(primary_number=PRIMARY)
        self.assertTrue(config.call_enabled and config.push_enabled)
        self.assertEqual((config.first_arm, config.arm_offset_seconds, config.cycle_seconds),
                         ("push", 30, 90))
        self.assertEqual((config.call_delay, config.push_delay), (30, 0))
        self.assertEqual(config.pushover_retry, 90)

    def test_push_stays_acknowledgeable_for_the_whole_incident(self):
        config = Config(primary_number=PRIMARY, backup_number=BACKUP)
        last_call = config.call_delay + (config.total_attempts - 1) * config.cycle_seconds
        self.assertGreater(config.pushover_expire, last_call + config.cycle_seconds)
        self.assertLessEqual(config.pushover_expire, 10800)

    def test_the_template_states_the_same_defaults_as_the_code(self):
        # template.yaml has to repeat each default; nothing else keeps the two in step.
        with open(os.path.join(os.path.dirname(__file__), "..", "template.yaml")) as template:
            text = template.read()
        env = {}
        for variable, parameter in re.findall(r"^ +([A-Z_]+): !Ref (\w+)$", text, re.M):
            block = re.search(r"^  %s:\n((?:    .*\n)+)" % parameter, text, re.M)
            default = re.search(r"^    Default: (.*)$", block.group(1), re.M) if block else None
            if default:
                env[variable] = default.group(1).strip("'\"")
        for variable in ("OWNER_NAME", "FIRST_ARM", "CYCLE_SECONDS", "BREAK_GLASS_MINUTES"):
            self.assertIn(variable, env)
        env["PRIMARY_NUMBER"] = PRIMARY  # no default: the call arm needs a number to ring
        self.assertEqual(Config.from_env(env=env), Config(primary_number=PRIMARY))


class PushFirst(unittest.TestCase):
    def test_push_goes_out_at_once_and_the_first_call_waits_for_the_offset(self):
        sim = make()
        sim.trigger()
        self.assertEqual(len(sim.notifier.emergencies), 1)
        self.assertEqual(len(sim.telephony.calls), 0)
        sim.run()
        self.assertEqual(times(sim, "push alert sent"), [0])
        self.assertEqual(times(sim, "placed to"), [30, 120, 210])

    def test_acknowledging_the_push_before_the_offset_means_no_call_at_all(self):
        sim = make()
        sim.push_ack_after = 12
        incident = sim.trigger()
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 0)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)
        self.assertEqual(sim.store.get(incident.id).acked_via, "pushover")

    def test_push_acknowledged_without_a_callback_is_caught_before_the_first_dial(self):
        sim = make()
        incident = sim.trigger()
        # Acknowledged in the app, but Pushover's callback never arrives.
        sim.notifier.acknowledged.add(sim.store.get(incident.id).pushover_receipt)
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 0)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)

    def test_acknowledging_the_push_while_the_phone_rings_hangs_up(self):
        sim = make()
        sim.push_ack_after = 40      # the first call started ringing at 30
        incident = sim.trigger()
        sim.run()
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(sim.telephony.hung_up, ["CA0001"])
        self.assertEqual(sim.store.get(incident.id).acked_via, "pushover")

    def test_pressing_1_retracts_the_push(self):
        sim = make(lambda n, to: ACK)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.notifier.cancelled, ["rcpt-%s" % incident.id])
        self.assertEqual(sim.telephony.hung_up, [])

    def test_offset_and_cycle_are_configurable(self):
        sim = make(arm_offset_seconds=20, cycle_seconds=60)
        sim.trigger()
        sim.run()
        self.assertEqual(times(sim, "placed to"), [20, 80, 140])
        self.assertEqual(sim.cfg.pushover_retry, 60)


class CallFirst(unittest.TestCase):
    def test_call_goes_out_at_once_and_the_push_waits_for_the_offset(self):
        sim = make(first_arm="call")
        sim.trigger()
        self.assertEqual(len(sim.telephony.calls), 1)
        self.assertEqual(len(sim.notifier.emergencies), 0)
        sim.run()
        self.assertEqual(times(sim, "placed to"), [0, 90, 180])
        self.assertEqual(times(sim, "push alert sent"), [30])

    def test_pressing_1_before_the_offset_means_no_push_at_all(self):
        sim = make(lambda n, to: ACK, first_arm="call")
        incident = sim.trigger()
        sim.run()
        self.assertEqual(len(sim.notifier.emergencies), 0)
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)

    def test_a_redelivered_push_message_does_not_send_twice(self):
        sim = make(first_arm="call")
        incident = sim.trigger()
        self.assertEqual(sim.service.deliver_push(incident.id), "sent")
        self.assertEqual(sim.service.deliver_push(incident.id), "already sent")
        self.assertEqual(len(sim.notifier.emergencies), 1)


class BothAtOnce(unittest.TestCase):
    def test_offset_zero_fires_both_immediately(self):
        sim = make(arm_offset_seconds=0)
        sim.trigger()
        self.assertEqual((len(sim.telephony.calls), len(sim.notifier.emergencies)), (1, 1))
        self.assertEqual(sim.scheduler.pushes, [])


class CallOnly(unittest.TestCase):
    def test_no_push_is_sent_and_calls_start_immediately(self):
        sim = make(push_enabled=False)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.notifier.emergencies, [])
        self.assertEqual(sim.notifier.lookups, 0)
        self.assertEqual(times(sim, "placed to"), [0, 90, 180])
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)


class PushOnly(unittest.TestCase):
    def test_no_call_is_placed_and_no_number_is_needed(self):
        sim = Simulation(Config(call_enabled=False, primary_attempts=3), lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        sim.run()
        self.assertEqual(sim.telephony.calls, [])
        self.assertEqual(len(sim.notifier.emergencies), 1)
        # Three cycles of 90 seconds, then it gives up and says so.
        self.assertEqual(sim.store.get(incident.id).status, EXPIRED)
        self.assertEqual(sim.store.get(incident.id).closed_at - incident.created_at, 270)
        self.assertEqual(sim.notifier.sender_messages[-1][1], "Nobody acknowledged your alert.")
        self.assertEqual(sim.notifier.cancelled, ["rcpt-%s" % incident.id])

    def test_acknowledgment_is_noticed_by_polling(self):
        sim = Simulation(Config(call_enabled=False), lambda n, to: NO_ANSWER)
        incident = sim.trigger()
        sim.notifier.acknowledged.add(sim.store.get(incident.id).pushover_receipt)
        sim.run()
        self.assertEqual(sim.store.get(incident.id).status, ACKNOWLEDGED)


class InvalidSettings(unittest.TestCase):
    def test_refused(self):
        bad = [
            dict(call_enabled=False, push_enabled=False),
            dict(primary_number=PRIMARY, first_arm="carrier pigeon"),
            dict(primary_number=PRIMARY, arm_offset_seconds=-1),
            dict(primary_number=PRIMARY, arm_offset_seconds=901),
            dict(primary_number=PRIMARY, cycle_seconds=29),    # Pushover's minimum repeat
            dict(primary_number=PRIMARY, cycle_seconds=901),   # SQS's maximum delay
            dict(primary_number=""),                           # call arm on, nobody to call
        ]
        for settings in bad:
            with self.assertRaises(ValueError, msg=str(settings)):
                Config(**settings)

    def test_flags_must_be_true_or_false(self):
        self.assertEqual(Config.arms_from_env({"CALL_ENABLED": "off"}), {"call": False, "push": True})
        with self.assertRaises(ValueError):
            Config.arms_from_env({"PUSH_ENABLED": "maybe"})


class Wiring(unittest.TestCase):
    def test_queued_push_message_reaches_deliver_push(self):
        sim = make(first_arm="call")
        handler.install(sim.service)
        incident = sim.trigger()
        event = {"Records": [{"body": json.dumps({"incident_id": incident.id, "action": "push"})}]}
        handler.lambda_handler(event)
        self.assertEqual(len(sim.notifier.emergencies), 1)
        self.assertEqual(sim.store.get(incident.id).status, ACTIVE)


if __name__ == "__main__":
    unittest.main()
