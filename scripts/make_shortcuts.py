#!/usr/bin/env python3
"""Build the sender's Shortcuts as signed files to AirDrop to their iPhone.

    python3 scripts/make_shortcuts.py --sender Robin --owner Alex

Writes Emergency, Emergency Backup and Emergency Drill into build/shortcuts/.
docs/shortcut.md describes what the shortcuts do and how to test them.

The files hold the sender's token and the Pushover keys, so they are written
readable by you alone. AirDrop them; never post or email them; delete them
afterwards. The secrets are read from SSM and are not printed.

Needs the AWS CLI, signed in to the account the stack is in, and macOS for
its `shortcuts` command, which signs the files so that an iPhone accepts
them. Use --placeholder to try the script without AWS, and --unsigned to
build without macOS (unsigned files cannot be imported).

If the backend refuses or fails, Emergency alerts the owner through Pushover
directly. That needs a Pushover application token inside the Shortcut. Give
the Shortcut one of its own, stored as <prefix>/shortcut-pushover-app-token
(or name another parameter with --pushover-token-param), so that a lost phone
means revoking that token and not the backend's. Without one the backend's
token is used and the script says so. On a deployment with no Pushover at
all, Emergency tells the sender to phone the owner instead, and Emergency
Backup is not built.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

OBJ = "￼"  # where a variable sits inside a piece of text
PUSHOVER = "https://api.pushover.net/1/messages.json"
ACT = "is.workflow.actions."
# The icon, as Shortcuts stores it: a background colour and a glyph number.
# Red for the two that alert for real, grey for the drill.
RED, GREY, GLYPH = 4282601983, 2846468607, 59771


def new_id():
    return str(uuid.uuid4()).upper()


class Var:
    """A reference to a named variable, an action's output, or the input."""

    def __init__(self, kind, name="", output_id="", prop=""):
        self.kind, self.name, self.output_id, self.prop = kind, name, output_id, prop

    def get(self, prop):
        return Var(self.kind, self.name, self.output_id, prop)

    def data(self):
        if self.kind == "named":
            d = {"Type": "Variable", "VariableName": self.name}
        elif self.kind == "input":
            d = {"Type": "ExtensionInput"}
        else:
            d = {"Type": "ActionOutput", "OutputUUID": self.output_id, "OutputName": self.name}
        if self.prop:
            d["Aggrandizements"] = [
                {"Type": "WFPropertyVariableAggrandizement", "PropertyName": self.prop}
            ]
        return d


def named(name):
    return Var("named", name)


def attach(var):
    return {"Value": var.data(), "WFSerializationType": "WFTextTokenAttachment"}


def text(*parts):
    """Text made of strings and variables."""
    string, attachments = "", {}
    for part in parts:
        if isinstance(part, Var):
            attachments["{%d, 1}" % len(string)] = part.data()
            string += OBJ
        else:
            string += part
    return {
        "Value": {"string": string, "attachmentsByRange": attachments},
        "WFSerializationType": "WFTextTokenString",
    }


def fields(pairs):
    return {
        "Value": {"WFDictionaryFieldValueItems": [
            {"WFItemType": 0, "WFKey": text(key), "WFValue": value if isinstance(value, dict) else text(value)}
            for key, value in pairs
        ]},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


class Flow:
    def __init__(self):
        self.actions = []

    def add(self, identifier, name="", **params):
        """Append an action; returns a reference to its output."""
        output_id = new_id()
        params.setdefault("UUID", output_id)
        self.actions.append({
            "WFWorkflowActionIdentifier": ACT + identifier,
            "WFWorkflowActionParameters": params,
        })
        return Var("output", name, output_id)

    # ---- small vocabulary

    def text(self, *parts):
        return self.add("gettext", "Text", WFTextActionText=text(*parts))

    def set(self, name, var):
        self.add("setvariable", WFVariableName=name, WFInput=attach(var))
        return named(name)

    def value(self, key, source):
        return self.add("getvalueforkey", "Dictionary Value", WFInput=attach(source),
                        WFDictionaryKey=key, WFGetDictionaryValueType="Value")

    def alert(self, title, message):
        self.add("alert", WFAlertActionTitle=title,
                 WFAlertActionMessage=message if isinstance(message, dict) else text(message),
                 WFAlertActionCancelButtonShown=False)

    def if_has_value(self, var):
        group = new_id()
        self.add("conditional", GroupingIdentifier=group, WFControlFlowMode=0, WFCondition=100,
                 WFInput={"Type": "Variable", "Variable": attach(var)})
        return group

    def otherwise(self, group):
        self.add("conditional", GroupingIdentifier=group, WFControlFlowMode=1)

    def end_if(self, group):
        self.add("conditional", GroupingIdentifier=group, WFControlFlowMode=2)

    def post(self, url, body_type, body, headers=None):
        params = dict(WFURL=url, WFHTTPMethod="POST", WFHTTPBodyType=body_type, ShowHeaders=True)
        params["WFJSONValues" if body_type == "JSON" else "WFFormValues"] = fields(body)
        if headers:
            params["WFHTTPHeaders"] = fields(headers)
        return self.add("downloadurl", "Contents of URL", **params)

    def get(self, url, headers):
        return self.add("downloadurl", "Contents of URL", WFURL=url, WFHTTPMethod="GET",
                        ShowHeaders=True, WFHTTPHeaders=fields(headers))


def pushover_alert(flow, message, cfg):
    """Send the owner a Pushover emergency alert directly and say how it went.
    Without Pushover there is nothing to send, and the sender is told so."""
    if not cfg.get("pushover_user"):
        flow.alert("Alert NOT sent", "Phone %s now." % cfg["owner"])
        return
    reply = flow.post(PUSHOVER, "Form", [
        ("token", cfg["pushover_token"]),
        ("user", cfg["pushover_user"]),
        ("title", "EMERGENCY from %s (backup)" % cfg["sender"]),
        ("message", text(message)),
        ("priority", "2"), ("retry", "30"), ("expire", "1800"),
        ("sound", cfg["sound"]),
    ])
    receipt = flow.value("receipt", reply)
    group = flow.if_has_value(receipt)
    flow.alert("Backup alert sent", "Now phone %s." % cfg["owner"])
    flow.otherwise(group)
    flow.alert("Alert NOT sent", "Phone %s now." % cfg["owner"])
    flow.end_if(group)


def backup(cfg):
    flow = Flow()
    message = flow.text("Emergency from %s. Call me now." % cfg["sender"])
    pushover_alert(flow, message, cfg)
    return flow.actions


def emergency(cfg, test=""):
    flow = Flow()
    auth = [("Authorization", "Bearer " + cfg["token"])]
    owner = cfg["owner"]

    menu = new_id()
    prompt = "What is the emergency?" if not test else "DRILL: what is the emergency?"
    if cfg.get("placeholder"):
        prompt = "PLACEHOLDER, alerts nobody. " + prompt
    flow.add("choosefrommenu", GroupingIdentifier=menu, WFControlFlowMode=0,
             WFMenuPrompt=prompt, WFMenuItems=["Kids", "Me", "House", "Cancel"])
    for title in ("Kids", "Me", "House"):
        flow.add("choosefrommenu", GroupingIdentifier=menu, WFControlFlowMode=1,
                 WFMenuItemTitle=title)
        flow.set("Kind", flow.text(title.lower()))
    flow.add("choosefrommenu", GroupingIdentifier=menu, WFControlFlowMode=1,
             WFMenuItemTitle="Cancel")
    flow.add("exit")
    flow.add("choosefrommenu", GroupingIdentifier=menu, WFControlFlowMode=2)
    kind = named("Kind")

    where = flow.add("getcurrentlocation", "Current Location")
    body = [("kind", text(kind)),
            ("lat", text(where.get("Latitude"))),
            ("lon", text(where.get("Longitude")))]
    if test:
        body.append(("test", test))
    reply = flow.post(cfg["url"] + "trigger", "JSON", body, auth)
    # Success is an "id" in the reply and nothing else. The backend leaves it
    # out whenever no alert went out (paused, refused, first alert failed),
    # and a reply that is not JSON has none either.
    incident = flow.value("id", reply)

    sent = flow.if_has_value(incident)
    flow.set("Incident", incident)
    # No timings here: which arms are on, and when the calls start, are the
    # deployment's settings and can change after this file is on the phone.
    flow.add("notification", WFNotificationActionTitle="Emergency Ring",
             WFNotificationActionBody=text("Alert sent. %s's phone is sounding." % owner))
    loop = new_id()
    flow.add("repeat.count", GroupingIdentifier=loop, WFControlFlowMode=0, WFRepeatCount=12)
    flow.add("delay", WFDelayTime=15)
    status = flow.get(text(cfg["url"] + "incidents/", named("Incident")), auth)
    final = flow.value("final", status)  # present only once the incident is over
    done = flow.if_has_value(final)
    flow.alert("Emergency Ring", text(final))
    flow.add("exit")
    flow.end_if(done)
    flow.add("repeat.count", GroupingIdentifier=loop, WFControlFlowMode=2)
    flow.alert("No response yet",
               "No acknowledgment after 3 minutes. %s's phone keeps alerting. "
               "Try another way to reach them too." % owner)

    flow.otherwise(sent)
    # The backend answered with something other than a started incident.
    if test:
        # A drill has no backup alert: sent straight to Pushover it would be
        # a real, unlabelled emergency that the backend can neither label
        # nor take back. So the Drill file needs no Pushover keys at all.
        flow.alert("Drill not started",
                   "The backend refused, is paused, or could not be reached. Nothing was sent.")
    else:
        message = flow.text(
            "Emergency from %s: " % cfg["sender"], kind,
            ". Call me now. https://maps.apple.com/?ll=",
            where.get("Latitude"), ",", where.get("Longitude"))
        pushover_alert(flow, message, cfg)
    flow.end_if(sent)
    return flow.actions


def workflow(actions, colour, glyph):
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "2607.0.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": colour, "WFWorkflowIconGlyphNumber": glyph},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
    }


class Missing(Exception):
    """An SSM parameter that does not exist."""


def aws(*args):
    try:
        done = subprocess.run(("aws",) + args, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("the aws command was not found: install the AWS CLI and sign in")
    except subprocess.CalledProcessError as error:
        # Only what aws wrote to stderr is shown: it says which call failed
        # and why. Its stdout could hold a parameter's value.
        why = (error.stderr or "").strip() or "exit status %d" % error.returncode
        if "ParameterNotFound" in why:
            raise Missing(why)
        sys.exit("aws %s failed: %s" % (" ".join(args[:2]), why))
    return done.stdout.strip()


def secret(region, name, required=True):
    """The value of an SSM parameter, or None for one that may be absent."""
    try:
        value = aws("ssm", "get-parameter", "--region", region, "--with-decryption",
                    "--name", name, "--query", "Parameter.Value", "--output", "text")
    except Missing:
        if required:
            sys.exit("there is no SSM parameter %s in %s" % (name, region))
        return None
    if not value:
        sys.exit("the SSM parameter %s in %s is empty" % (name, region))
    return value


def backend_url(value, stack):
    """The address the Shortcut sends the sender's token to, checked.

    describe-stacks prints "None" or nothing for a stack without the output,
    and a Shortcut built around that would fail on the phone without ever
    reaching its fallback."""
    try:
        parts = urlsplit(value)
        parts.port  # raises for a port that is not a number
        plain = (parts.scheme == "https" and "." in (parts.hostname or "")
                 and not parts.username and not parts.query and not parts.fragment
                 and not any(c.isspace() for c in value))
    except ValueError:
        plain = False
    if not plain:
        sys.exit("the stack %s has no usable FunctionUrl output: expected an "
                 "https address, got %r" % (stack, value))
    return value if value.endswith("/") else value + "/"


def stack_sound(args):
    """The alert sound the stack itself uses, so that the backup alert sounds
    like the real one. A stack that does not say gets Pushover's long alarm."""
    value = aws(
        "cloudformation", "describe-stacks", "--region", args.region, "--stack-name",
        args.stack, "--query", "Stacks[0].Parameters[?ParameterKey=='PushSound'].ParameterValue",
        "--output", "text")
    return value if value and value != "None" and not any(c.isspace() for c in value) else "persistent"


def settings(args):
    """What goes into the Shortcuts, read from the deployed stack."""
    cfg = {"sender": args.sender, "owner": args.owner,
           "sound": args.sound or stack_sound(args)}
    prefix = args.prefix.rstrip("/")
    cfg["url"] = backend_url(aws(
        "cloudformation", "describe-stacks", "--region", args.region, "--stack-name",
        args.stack, "--query", "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue",
        "--output", "text"), args.stack)
    try:
        tokens = json.loads(secret(args.region, prefix + "/trigger-tokens"))
    except ValueError:
        tokens = None
    if not isinstance(tokens, dict):
        sys.exit("%s/trigger-tokens is not a JSON object of sender names and tokens" % prefix)
    if args.sender not in tokens:
        sys.exit("no sender %r in trigger-tokens; have: %s" % (args.sender, ", ".join(tokens)))
    cfg["token"] = tokens[args.sender]
    if not isinstance(cfg["token"], str) or not cfg["token"]:
        sys.exit("the token for %r in trigger-tokens is empty" % args.sender)

    # A deployment with the push arm off has no Pushover parameters, and the
    # Shortcut can do without its direct alert; the sender is told to phone.
    cfg["pushover_user"] = secret(args.region, prefix + "/pushover-user-key", required=False)
    cfg["pushover_token"] = None
    if not cfg["pushover_user"]:
        print("note: no %s/pushover-user-key, so the Shortcut has no Pushover fallback: "
              "if the backend fails, Emergency tells %s to phone %s, and Emergency "
              "Backup is not built." % (prefix, args.sender, args.owner),
              file=sys.stderr)
        return cfg
    own = args.pushover_token_param or prefix + "/shortcut-pushover-app-token"
    cfg["pushover_token"] = secret(args.region, own, required=bool(args.pushover_token_param))
    if not cfg["pushover_token"]:
        cfg["pushover_token"] = secret(args.region, prefix + "/pushover-app-token")
        print("warning: no %s, so the Shortcut carries the backend's own Pushover "
              "application token. If %s's phone is lost, that token has to be replaced "
              "for the backend too. Create a second Pushover application for the "
              "Shortcut and store its token there to keep the two apart." % (own, args.sender),
              file=sys.stderr)
    return cfg


def placeholders(args):
    return {"sender": args.sender, "owner": args.owner, "sound": args.sound or "persistent",
            "url": "https://example.invalid/", "token": "placeholder",
            "pushover_token": "placeholder", "pushover_user": "placeholder",
            "placeholder": True}


def shortcuts(cfg):
    """Each Shortcut's name and content."""
    # A placeholder build alerts nobody, and its file names say so.
    label = "PLACEHOLDER " if cfg.get("placeholder") else ""
    files = {label + "Emergency": workflow(emergency(cfg), RED, GLYPH)}
    if cfg.get("pushover_user"):
        files[label + "Emergency Backup"] = workflow(backup(cfg), RED, GLYPH)
    files[label + "Emergency Drill"] = workflow(emergency(cfg, test="drill"), GREY, GLYPH)
    return files


def sign(raw, final):
    try:
        subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", raw, "--output", final],
                       check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("signing needs the `shortcuts` command, which comes with macOS; "
                 "--unsigned builds without it")
    except subprocess.CalledProcessError as error:
        sys.exit("could not sign %s: %s\nFiles already written to %s hold secrets: delete that folder." % (
            os.path.basename(final),
            (error.stderr or "").strip() or "exit status %d" % error.returncode,
            os.path.dirname(final) or "."))


def write(files, out, signed=True):
    """Write each Shortcut into `out`, readable by this user alone, and
    return the paths. The unsigned file holds the same secrets as the signed
    one, so it goes whether or not signing worked."""
    os.makedirs(out, exist_ok=True)
    written = []
    for name, content in files.items():
        raw = os.path.join(out, name + ".unsigned.shortcut")
        final = os.path.join(out, name + ".shortcut")
        for path in (raw, final):
            # A file left by an earlier run keeps the permissions it had.
            if os.path.exists(path):
                os.remove(path)
        try:
            with open(os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                plistlib.dump(content, handle, fmt=plistlib.FMT_BINARY)
            if signed:
                sign(raw, final)
                os.chmod(final, 0o600)
        finally:
            if signed and os.path.exists(raw):
                os.remove(raw)
        written.append(final if signed else raw)
    return written


def parser():
    made = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    made.add_argument("--sender", required=True,
                      help="who presses the button: their name in trigger-tokens, "
                           "shown to the owner in the alert")
    made.add_argument("--owner", required=True,
                      help="whose phone is alerted; used in what the sender is shown")
    made.add_argument("--out", default="build/shortcuts", metavar="DIR",
                      help="folder to write the files into (default: %(default)s)")
    made.add_argument("--sound",
                      help="Pushover sound of the Shortcut's own backup alert "
                           "(default: the stack's PushSound, so it sounds like the real alert)")
    made.add_argument("--stack", default="emergency-ring",
                      help="CloudFormation stack to read the backend address from "
                           "(default: %(default)s)")
    made.add_argument("--region", default="us-west-2",
                      help="AWS region of the stack and its parameters (default: %(default)s)")
    made.add_argument("--prefix", default="/emergency-ring",
                      help="where the stack's SSM parameters live (default: %(default)s)")
    made.add_argument("--pushover-token-param", metavar="NAME",
                      help="SSM parameter holding a Pushover application token for the "
                           "Shortcut alone (default: <prefix>/shortcut-pushover-app-token, "
                           "and the backend's token, with a warning, if that is absent)")
    made.add_argument("--placeholder", action="store_true",
                      help="use made-up secrets and no AWS, to check that the files import")
    made.add_argument("--unsigned", action="store_true",
                      help="skip signing and keep the .unsigned.shortcut files, which "
                           "hold the same secrets and cannot be imported")
    return made


def main(argv=None):
    # Everything written from here on holds secrets: the folder, the unsigned
    # files, and the files the signing tool creates. This covers all three.
    os.umask(0o077)
    args = parser().parse_args(argv)
    cfg = placeholders(args) if args.placeholder else settings(args)
    for path in write(shortcuts(cfg), args.out, signed=not args.unsigned):
        print("wrote", path)
    if cfg.get("pushover_user"):
        print("backup alert sound:", cfg["sound"])
    if args.placeholder:
        print("These are placeholders: they alert nobody. Do not leave them on a phone.")
        return
    if args.unsigned:
        print("These are unsigned, so an iPhone will not take them, but they hold the "
              "same secrets:\ndelete the folder %s when you are done." % args.out)
        return
    print("AirDrop these to %s's iPhone. They hold secrets, so afterwards:\n"
          "  - delete the folder %s\n"
          "  - delete any copy you imported into Shortcuts on this Mac to test it;\n"
          "    while it is there, iCloud carries it to your other devices"
          % (args.sender, args.out))


if __name__ == "__main__":
    main()
