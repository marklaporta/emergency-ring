"""scripts/simulate.py is the documented way to watch the logic play out, so
every scenario it offers has to run and end the way its name says.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import simulate  # noqa: E402

OWNER, BACKUP = simulate.OWNER, simulate.BACKUP

# name: (how it ends, who was called in order, how it was acknowledged,
#        what the sender is told)
ENDINGS = {
    "push-ack": ("acknowledged", [], "pushover", "The owner acknowledged your alert."),
    "push-ack-ringing": ("acknowledged", [OWNER], "pushover", "The owner acknowledged your alert."),
    "answered": ("acknowledged", [OWNER] * 3, "call", "The owner acknowledged your alert."),
    "voicemail": ("acknowledged", [OWNER] * 2, "call", "The owner acknowledged your alert."),
    "escalate": ("acknowledged", [OWNER] * 3 + [BACKUP], "call",
                 "The backup contact acknowledged your alert."),
    "nobody": ("expired", [OWNER] * 3 + [BACKUP] * 2, None,
               "Nobody acknowledged your alert after 5 calls."),
    "lost-callback": ("acknowledged", [OWNER] * 2, "call", "The owner acknowledged your alert."),
    "false-alarm": ("cancelled", [], None, None),
    "double-tap": ("acknowledged", [OWNER], "call", "The owner acknowledged your alert."),
    "call-first": ("acknowledged", [OWNER] * 2, "call", "The owner acknowledged your alert."),
    "at-once": ("acknowledged", [OWNER] * 2, "call", "The owner acknowledged your alert."),
    "call-only": ("acknowledged", [OWNER] * 2, "call", "The owner acknowledged your alert."),
    "push-only": ("acknowledged", [], "pushover", "The owner acknowledged your alert."),
}


def incident(sim):
    return sim.store.get(next(iter(sim.store.items)))


class ScenarioTest(unittest.TestCase):
    def test_every_scenario_has_an_expected_ending_here(self):
        # A scenario added to the script without a line above would go unchecked.
        self.assertEqual(sorted(simulate.SCENARIOS), sorted(ENDINGS))

    def test_each_scenario_ends_the_way_its_name_says(self):
        for name, (status, called, via, told) in ENDINGS.items():
            with self.subTest(scenario=name):
                sim = simulate.run(name)
                # One incident, however often the sender pressed.
                self.assertEqual(len(sim.store.items), 1)
                final = incident(sim)
                self.assertEqual(final.status, status)
                self.assertEqual([call["to"] for call in sim.telephony.calls], called)
                self.assertEqual(final.attempts, len(called))
                self.assertEqual(final.acked_via, via)
                self.assertEqual([text for _, text in sim.notifier.sender_messages],
                                 [told] if told else [])
                # Settled: no timer is left that could alert again.
                self.assertEqual(sim.scheduler.queue, [])
                self.assertEqual(sim.scheduler.pushes, [])
                # Whatever push went out was taken back when it ended.
                self.assertEqual(sim.notifier.cancelled, sim.notifier.emergencies)

    def test_the_arms_each_scenario_names_are_the_ones_used(self):
        self.assertEqual(simulate.run("call-only").notifier.emergencies, [])
        self.assertEqual(simulate.run("push-only").telephony.calls, [])
        self.assertEqual(len(simulate.run("push-only").notifier.emergencies), 1)
        # Which arm goes first is the second line of the story.
        self.assertIn("push alert sent", simulate.run("answered").log[1])
        self.assertIn("call 1 placed", simulate.run("call-first").log[1])
        at_once = simulate.run("at-once").log
        self.assertTrue(at_once[1].startswith("t+  0s") and at_once[2].startswith("t+  0s"),
                        at_once[:3])

    def test_an_acknowledged_push_hangs_up_the_call_that_is_ringing(self):
        self.assertEqual(len(simulate.run("push-ack-ringing").telephony.hung_up), 1)
        self.assertEqual(simulate.run("push-ack").telephony.hung_up, [])

    def test_voicemail_is_not_taken_for_an_answer(self):
        log = "\n".join(simulate.run("voicemail").log)
        self.assertIn("voicemail answered; no keypress, so not acknowledged", log)
        self.assertIn("call 2 placed", log)

    def test_a_lost_callback_does_not_stop_the_next_call(self):
        log = "\n".join(simulate.run("lost-callback").log)
        self.assertIn("status callback lost", log)
        self.assertIn("call 2 placed", log)

    def test_a_second_press_joins_the_first_incident(self):
        self.assertIn("attached to active incident", simulate.run("double-tap").log[2])

    def test_the_false_alarm_is_cancelled_before_any_call_is_due(self):
        sim = simulate.run("false-alarm")
        cancelled_at = next(line for line in sim.log if "Robin cancels" in line)
        self.assertTrue(cancelled_at.startswith("t+ 20s"), cancelled_at)
        # The first call was due later than that, so the story leaves none out.
        self.assertGreater(sim.cfg.arm_offset_seconds, 20)


class CommandLineTest(unittest.TestCase):
    def run_main(self, *names):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = simulate.main(["simulate.py"] + list(names))
        return code, out.getvalue()

    def test_no_argument_plays_every_scenario(self):
        code, out = self.run_main()
        self.assertEqual(code, 0)
        for name in ENDINGS:
            self.assertIn("== %s: %s ==" % (name, simulate.SCENARIOS[name][0]), out)
        self.assertEqual(out.count("  result: "), len(ENDINGS))

    def test_one_scenario_by_name(self):
        code, out = self.run_main("nobody")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("\n== "), 1)
        self.assertIn("  result: expired after 5 call(s)\n", out)
        self.assertIn("  Robin is told: Nobody acknowledged your alert after 5 calls.\n", out)

    def test_an_unknown_name_lists_the_choices(self):
        code, out = self.run_main("nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown scenario 'nope'", out)
        for name in ENDINGS:
            self.assertIn(name, out)


if __name__ == "__main__":
    unittest.main()
