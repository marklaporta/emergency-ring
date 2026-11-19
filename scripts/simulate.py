#!/usr/bin/env python3
"""Play out incidents in virtual time against the real logic, with Twilio,
Pushover, DynamoDB and SQS faked. Nothing leaves this machine.

    python3 scripts/simulate.py            # all scenarios
    python3 scripts/simulate.py escalate   # one scenario

Unless a scenario says otherwise it uses the default settings: both arms on,
push first, the first call 30 seconds later, each arm repeating every 90.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from er.config import Config  # noqa: E402
from er.fakes import ACK, LOST_CALLBACK, NO_ANSWER, VOICEMAIL, Simulation  # noqa: E402

OWNER = "+1-555-OWNER"
BACKUP = "+1-555-BACKUP"


def config(**overrides):
    settings = dict(
        primary_number=OWNER, backup_number=BACKUP, primary_attempts=3, backup_attempts=2,
        # Not a secret: without a sender the configuration check logs an error.
        trigger_tokens={"Robin": "simulated-token-not-a-secret"},
    )
    settings.update(overrides)
    return Config(**settings)


def plain(sim):
    sim.trigger(kind="kids", note="Call me now.")
    sim.run()


def ack_push_after(seconds):
    def play(sim):
        sim.push_ack_after = seconds
        sim.trigger(kind="kids", note="Call me now.")
        sim.run()
    return play


def false_alarm(sim):
    incident = sim.trigger(kind="kids")
    # Before the first call is due: skipping the clock past it would leave
    # out the call a later cancel has to hang up.
    sim.clock.t += 20
    sim.service.cancel(incident.id, "Robin")
    sim.say("Robin cancels: false alarm")
    sim.run()


def double_tap(sim):
    sim.trigger(kind="kids")
    sim.clock.t += 5
    sim.trigger(kind="kids")
    sim.run()


# name: (title, settings, what the person does with call N, how it plays out)
SCENARIOS = {
    "push-ack": (
        "The push is acknowledged before the first call",
        {}, lambda n, to: NO_ANSWER, ack_push_after(12),
    ),
    "push-ack-ringing": (
        "The push is acknowledged while the phone is ringing",
        {}, lambda n, to: NO_ANSWER, ack_push_after(40),
    ),
    "answered": (
        "The push is missed, two calls ring out, the third is answered",
        {}, lambda n, to: ACK if n == 3 else NO_ANSWER, plain,
    ),
    "voicemail": (
        "Voicemail answers the first call",
        {}, lambda n, to: VOICEMAIL if n == 1 else ACK, plain,
    ),
    "escalate": (
        "The owner never responds; the backup contact answers",
        {}, lambda n, to: ACK if to == BACKUP else NO_ANSWER, plain,
    ),
    "nobody": (
        "Nobody responds at all",
        {}, lambda n, to: NO_ANSWER, plain,
    ),
    "lost-callback": (
        "Twilio's status callback goes missing",
        {}, lambda n, to: LOST_CALLBACK if n == 1 else ACK, plain,
    ),
    "false-alarm": (
        "Robin cancels before the first call",
        {}, lambda n, to: NO_ANSWER, false_alarm,
    ),
    "double-tap": (
        "Robin triggers twice in a panic",
        {}, lambda n, to: ACK if n == 1 else NO_ANSWER, double_tap,
    ),
    "call-first": (
        "Calls first, push 30 seconds later",
        {"first_arm": "call"}, lambda n, to: ACK if n == 2 else NO_ANSWER, plain,
    ),
    "at-once": (
        "Both arms fire together (offset 0)",
        {"arm_offset_seconds": 0}, lambda n, to: ACK if n == 2 else NO_ANSWER, plain,
    ),
    "call-only": (
        "Push arm switched off",
        {"push_enabled": False}, lambda n, to: ACK if n == 2 else NO_ANSWER, plain,
    ),
    "push-only": (
        "Call arm switched off; acknowledged in the app after two minutes",
        {"call_enabled": False, "primary_number": "", "backup_number": None},
        lambda n, to: NO_ANSWER, ack_push_after(120),
    ),
}


def run(name):
    """Play one scenario to its end and return the simulation."""
    title, settings, behaviour, play = SCENARIOS[name]
    sim = Simulation(config(**settings), behaviour)
    play(sim)
    return sim


def main(argv):
    names = argv[1:] or list(SCENARIOS)
    for name in names:
        if name not in SCENARIOS:
            print("unknown scenario %r; choose from: %s" % (name, ", ".join(SCENARIOS)))
            return 2
        sim = run(name)
        print("\n== %s: %s ==" % (name, SCENARIOS[name][0]))
        for line in sim.log:
            print("  " + line)
        final = sim.store.get(next(iter(sim.store.items)))
        print("  result: %s after %d call(s)" % (final.status, final.attempts))
        for sender, text in sim.notifier.sender_messages:
            print("  %s is told: %s" % (sender, text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
