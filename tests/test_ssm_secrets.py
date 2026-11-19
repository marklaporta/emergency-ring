import unittest

from support import PRIMARY, ROBIN

from er.config import Config
from er.ssm_secrets import load_secrets, required_for

CALLS_ONLY = required_for(True, False)


class StubSsm:
    def __init__(self, parameters):
        self.parameters = parameters
        self.calls = []

    def get_parameters(self, Names, WithDecryption=False):
        self.calls.append({"Names": Names, "WithDecryption": WithDecryption})
        return {
            "Parameters": [
                {"Name": name, "Type": "SecureString", "Value": self.parameters[name]}
                for name in Names if name in self.parameters
            ],
            "InvalidParameters": [name for name in Names if name not in self.parameters],
        }


EVERYTHING = {
    "/emergency-ring/twilio-auth-token": "twilio-secret",
    "/emergency-ring/trigger-tokens": '{"Robin": "robin-token"}',
    "/emergency-ring/pushover-app-token": "app-token",
    "/emergency-ring/pushover-user-key": "user-key",
}


class LoadSecrets(unittest.TestCase):
    def test_reads_every_parameter_decrypted_in_one_call(self):
        ssm = StubSsm(EVERYTHING)
        secrets = load_secrets("/emergency-ring", client=ssm, required=CALLS_ONLY)
        self.assertEqual(
            secrets,
            {
                "twilio-auth-token": "twilio-secret",
                "trigger-tokens": '{"Robin": "robin-token"}',
                "pushover-app-token": "app-token",
                "pushover-user-key": "user-key",
            },
        )
        self.assertEqual(len(ssm.calls), 1)
        self.assertTrue(ssm.calls[0]["WithDecryption"])
        # The optional agent tokens are asked for too; they are just absent here.
        self.assertEqual(
            sorted(ssm.calls[0]["Names"]),
            sorted(list(EVERYTHING) + ["/emergency-ring/break-glass-tokens"]),
        )

    def test_trailing_slash_on_the_prefix_is_ignored(self):
        secrets = load_secrets("/emergency-ring/", client=StubSsm(EVERYTHING), required=CALLS_ONLY)
        self.assertEqual(secrets["twilio-auth-token"], "twilio-secret")

    def test_pushover_parameters_are_optional(self):
        only_required = {k: v for k, v in EVERYTHING.items() if "pushover" not in k}
        secrets = load_secrets("/emergency-ring", client=StubSsm(only_required), required=CALLS_ONLY)
        self.assertEqual(sorted(secrets), ["trigger-tokens", "twilio-auth-token"])

    def test_missing_required_parameter_raises_and_names_it(self):
        without_tokens = dict(EVERYTHING)
        del without_tokens["/emergency-ring/trigger-tokens"]
        with self.assertRaises(RuntimeError) as caught:
            load_secrets("/emergency-ring", client=StubSsm(without_tokens), required=CALLS_ONLY)
        self.assertIn("/emergency-ring/trigger-tokens", str(caught.exception))
        self.assertNotIn("twilio-secret", str(caught.exception))

    def test_prefix_must_be_a_path(self):
        with self.assertRaises(RuntimeError):
            load_secrets("emergency-ring", client=StubSsm(EVERYTHING), required=CALLS_ONLY)

    def test_required_secrets_follow_the_arms(self):
        self.assertEqual(required_for(True, True),
                         ("trigger-tokens", "twilio-auth-token",
                          "pushover-app-token", "pushover-user-key"))
        self.assertEqual(required_for(True, False), ("trigger-tokens", "twilio-auth-token"))
        self.assertEqual(required_for(False, True),
                         ("trigger-tokens", "pushover-app-token", "pushover-user-key"))

    def test_push_arm_on_with_a_missing_pushover_secret_fails_loudly(self):
        without_pushover = {k: v for k, v in EVERYTHING.items() if "pushover" not in k}
        with self.assertRaises(RuntimeError) as raised:
            load_secrets("/emergency-ring", client=StubSsm(without_pushover),
                         required=required_for(True, True))
        self.assertIn("/emergency-ring/pushover-app-token", str(raised.exception))


class ConfigSecrets(unittest.TestCase):
    def test_from_env_takes_secrets_as_arguments_not_from_the_environment(self):
        config = Config.from_env(
            trigger_tokens='{"Robin": "%s"}' % ROBIN, twilio_auth_token="twilio-secret",
            env={"PRIMARY_NUMBER": PRIMARY, "TRIGGER_TOKENS": '{"Mallory": "from-env"}'},
        )
        self.assertEqual(config.trigger_tokens, {"Robin": ROBIN})
        self.assertEqual(config.twilio_auth_token, "twilio-secret")

    def test_secrets_stay_out_of_the_config_repr(self):
        config = Config(
            primary_number=PRIMARY, trigger_tokens={"Robin": ROBIN}, twilio_auth_token="twilio-secret",
        )
        self.assertNotIn(ROBIN, repr(config))
        self.assertNotIn("twilio-secret", repr(config))


if __name__ == "__main__":
    unittest.main()
