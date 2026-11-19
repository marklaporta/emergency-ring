# The sender's Shortcut

The sender's side is two shortcuts on their iPhone. In the example text below
the sender is called Robin and the owner, whose phone is alerted, is called
Alex:

- **Emergency** is the one on their Home Screen. It asks Kids / Me / House /
  Cancel, gets their location, and POSTs to the backend. It then checks back
  every 15 seconds for three minutes and tells them when the owner has
  acknowledged. If the backend answers with anything other than a started
  incident, it falls back to a direct alert. The three kinds are examples:
  the backend takes any short text as the kind, so change the menu to suit
  the household, after import or in step 2 of the hand build.
- **Emergency Backup** sends a Pushover emergency alert straight to the owner,
  without the backend. It is the same alert as the fallback, for the sender to
  tap when Emergency itself gets no answer.

There are two ways to make them: generate the files on a Mac, or build them by
hand on the phone.

## What has been run on a phone

- The generated **Emergency** and **Emergency Drill** have been run on a
  sender's iPhone against a live deployment. That was before this release's
  changes to the generator, to the scripts and to the backend's replies; the
  files it writes now have been checked by the unit tests only.
- The generated fallback branch, the direct Pushover alert when the backend
  answers with an error, has not been run on a phone yet. The generated
  Emergency Backup is built from the same actions. Do test steps 1 and 5 below
  before relying on either.
- The hand-built steps describe the same logic and were written from the
  Shortcuts action list. They have not been followed click by click on a
  device.

## The quick way: generate them

On a Mac, with the AWS CLI signed in to the account the stack is in:

    python3 scripts/make_shortcuts.py --sender Robin --owner Alex

This writes signed **Emergency**, **Emergency Backup** and **Emergency Drill**
files to `build/shortcuts/`. It needs macOS because the files are signed with
the `shortcuts` command that comes with it; an iPhone does not accept unsigned
ones. `python3 scripts/make_shortcuts.py --help` lists the options:

| Option | Default | What it sets |
|---|---|---|
| `--stack` | `emergency-ring` | The CloudFormation stack to read the backend address from |
| `--region` | `us-west-2` | The region of the stack and its secrets |
| `--prefix` | `/emergency-ring` | Where the stack's secrets live in SSM Parameter Store |
| `--sound` | The stack's `PushSound` | The Pushover sound of the Shortcut's own backup alert. The script prints the sound it used. |
| `--pushover-token-param` | `<prefix>/shortcut-pushover-app-token` | The SSM parameter holding a Pushover application token for the Shortcut alone |
| `--out` | `build/shortcuts` | The folder the files are written into |
| `--placeholder` | off | Made-up secrets and no AWS, to check that the files import. These alert nobody and are named PLACEHOLDER. |
| `--unsigned` | off | Skip signing. The files hold the same secrets and cannot be imported. |

**A Pushover token for the Shortcut alone.** Emergency and Emergency Backup
carry a Pushover application token for their direct alert. Create a second
application on pushover.net and store its token as the SecureString
`/emergency-ring/shortcut-pushover-app-token`, the same way as the other
[secrets](deploy.md#secrets). The generator then uses it. Without it the
generator embeds the backend's own `pushover-app-token` and prints a warning:
if that phone is lost, the backend's token has to be replaced too.

AirDrop the files to the sender's iPhone, tap **Add Shortcut** on each, add
Emergency and Emergency Backup to the Home Screen, then do the test section at
the end.

Emergency and Emergency Backup hold the sender's token and the Pushover keys.
Emergency Drill holds the sender's token only. Afterwards:

- Delete `build/shortcuts/`.
- If you imported Emergency Drill into Shortcuts on the Mac to try the whole
  path first, delete that copy too. While it is there it holds the token, and
  iCloud carries it to your other devices.

The same goes for the sender: a shortcut syncs through iCloud to their other
devices, so the token is on each of them, and anyone who can open Shortcuts
there can raise an alert.

### How the generated shortcuts differ from the hand-built ones

- **Fallback.** The generated Emergency sends the Pushover alert itself. The
  hand-built one runs Emergency Backup and passes it the message, so the
  hand-built Emergency Backup accepts input and the generated one does not.
- **Status loop.** The generated one looks for the `final` field, which the
  backend includes only once an incident is over. The hand-built one compares
  `status` with `active`.
- **Location.** The generated one leaves the location action at its default
  precision. The hand-built steps set **Nearest Hundred Meters**, which
  returns sooner.
- **Wording.** The generated ones name the owner ("Alex's phone is sounding",
  "Now phone Alex"). The hand-built text says "their".
- **Drill.** The generated Emergency Drill asks "DRILL: what is the
  emergency?" and has a grey icon. It has no backup alert: if the backend
  answers with anything other than a started drill (a refusal, a pause, an
  error page), it shows "Drill not started", says that nothing was sent, and
  sends nothing. A backup alert from a drill would go straight to Pushover as
  a real, unlabelled emergency that the backend could neither label nor take
  back.
- **Pushover token.** The generator reads the keys from the stack's SSM
  parameters, and prefers an application token of the Shortcut's own, as
  described above.
- **Backup alert sound.** The generator uses the stack's `PushSound` unless
  `--sound` names another.
- **No push arm.** On a deployment without Pushover the generator builds no
  Emergency Backup, and Emergency tells the sender to phone the owner when the
  backend fails.

## What the fallback can and cannot do

Shortcuts has no timeout setting on **Get Contents of URL** and no way to
catch its failure, so "fall back if the backend has not answered in a few
seconds" cannot be built as one shortcut:

| What happens | What the sender sees |
|---|---|
| Backend answers with a started incident | "Alert sent" |
| Backend answers with anything else (broken deploy, bad token, AWS error page, alerts paused, nothing has reached the owner's phone yet) | Fallback runs by itself, usually within a second or two |
| The request hangs, or fails with no answer (no signal, DNS, AWS unreachable) | A spinner, then an iOS error after up to a minute. **No fallback runs.** |

For the third row the rule for the sender is: **if "Alert sent" has not appeared
after five seconds, tap Emergency Backup, then phone the owner.** That is why the
backup is its own shortcut and sits next to the main one. With no signal at
all, neither shortcut can work.

An alert sent by the fallback or by Emergency Backup is not known to the
backend: it places no call, and acknowledging it tells the sender nothing. It
repeats every 30 seconds until the owner acknowledges it in Pushover, for 30
minutes at most. A pause of the backend does not block it.

When the backend has an incident running but nothing has reached the phone
yet, it answers without an `id` on purpose, so the fallback runs as well. The
owner may then get both the direct alert and, once the backend's retry works,
the backend's own.

## What happens on the owner's side

With the default settings: their phone sounds a loud alert at once; if it is
not acknowledged within 30 seconds the system starts phoning them; both repeat
every 90 seconds until one of them is acknowledged, or for about eight
minutes. The shortcut stops watching after three minutes and says "No
response yet". After that the sender hears how it ended only if the backend
has their Pushover key (`SenderPushoverKeys`, in [deploy.md](deploy.md#alarms-sender-receipts-break-glass)).

## Building by hand

The steps below use the iOS 18 names of the actions; they move around a
little between versions.

### Before you start

Have these ready on the sender's phone. Pass them over in person or by AirDrop, not
by text message or email.

| Value | Where it comes from |
|---|---|
| Function URL | The `FunctionUrl` output of `sam deploy`. Ends in `.on.aws/`. |
| The sender's bearer token | Their entry in the `/emergency-ring/trigger-tokens` parameter. |
| Pushover application token | pushover.net, the application's page. |
| The owner's Pushover user key | pushover.net dashboard, top right. The alert goes to the owner, so it is their key, not the sender's. |

A second Pushover application just for the shortcuts is worth having: its
token can then be replaced without touching the backend's.

On the sender's phone, in Settings > Apps > Shortcuts > Location, choose **While
Using the App** and leave **Precise Location** on.

### Shortcut 1: Emergency Backup

Build this one first, because Emergency refers to it.

1. Shortcuts app > **+**. Rename it **Emergency Backup**.
2. Add **Text**. Contents: `Emergency from Robin. Call me now.` (use the sender's own name)
3. Add **Set Variable**: name `Message`, value the **Text** above.
4. Add **If**. Set it to **Shortcut Input** / **has any value**. Inside it add
   **Set Variable**: name `Message`, value **Shortcut Input**. Delete the
   **Otherwise** branch.

   When Emergency runs this shortcut it passes its own message in, with the
   kind and a map link. When the sender taps it directly there is no input and the
   default from step 2 is sent.
5. Add **Get Contents of URL**.
   - URL: `https://api.pushover.net/1/messages.json`
   - Tap **Show More**.
   - Method: **POST**
   - Request Body: **Form**
   - Add these fields, all of type **Text**:

     | Key | Value |
     |---|---|
     | `token` | the Pushover application token |
     | `user` | The owner's Pushover user key |
     | `title` | `EMERGENCY from Robin (backup)` |
     | `message` | the **Message** variable |
     | `priority` | `2` |
     | `retry` | `30` |
     | `expire` | `1800` |
     | `sound` | `persistent` |

   `priority` 2 is Pushover's emergency level: it repeats every `retry` seconds
   until the owner acknowledges it or `expire` seconds pass. Pushover rejects a
   `retry` under 30 or an `expire` over 10800. `sound` is any Pushover sound
   name; use the one the owner chose for `PushSound`.
6. Add **Get Dictionary Value**: **Value** for key `receipt` in **Contents of
   URL**.
7. Add **If**: **Dictionary Value** / **has any value**.
   - Inside **If**: **Show Alert**. Title `Backup alert sent`, message
     `Now phone them.` Turn **Show Cancel Button** off.
   - Inside **Otherwise**: **Show Alert**. Title `Alert NOT sent`, message
     `Phone them now.` Turn **Show Cancel Button** off.

### Shortcut 2: Emergency

1. Shortcuts app > **+**. Rename it **Emergency**.
2. Add **Choose from Menu**. Prompt: `What is the emergency?` Rename the items
   and add two more so there are four, in this order: `Kids`, `Me`, `House`,
   `Cancel`. Kids, Me and House are examples; use the kinds that fit, and
   keep `Cancel` last.
3. Fill in each branch of the menu:
   - Under **Kids**: add **Text** with contents `kids`
   - Under **Me**: add **Text** with contents `me`
   - Under **House**: add **Text** with contents `house`
   - Under **Cancel**: add **Stop This Shortcut**
4. After **End Menu**, add **Set Variable**: name `Kind`, value **Menu Result**.
5. Add **Get Current Location**. Tap the arrow on the action and set
   **Precision** to **Nearest Hundred Meters**. It returns much faster than
   **Best** and is enough to open a map in the right place.
6. Add **Get Contents of URL**.
   - URL: the Function URL followed by `trigger`, for example
     `https://abc123.lambda-url.us-west-2.on.aws/trigger`
   - Tap **Show More**.
   - Method: **POST**
   - Headers: add one. Key `Authorization`, value `Bearer ` (with the space)
     followed by the sender's token.
   - Request Body: **JSON**
   - Add these fields, all of type **Text**:

     | Key | Value |
     |---|---|
     | `kind` | the **Kind** variable |
     | `lat` | the **Current Location** variable; tap it and choose **Latitude** |
     | `lon` | the **Current Location** variable; tap it and choose **Longitude** |

   Shortcuts adds the `Content-Type: application/json` header itself. Text is
   deliberate for `lat` and `lon`: the backend accepts either and Text avoids
   the number field rounding them.
7. Add **Get Dictionary Value**: **Value** for key `id` in **Contents of URL**.
   The backend returns an `id` for a new incident (HTTP 201) and for one that
   is already active (HTTP 200). Every other reply has no `id`: an error, a
   pause, or a real alert that has not reached the phone yet (HTTP 502).
8. Add **If**: **Dictionary Value** / **has any value**.
   - Inside **If**:
     1. **Set Variable**: name `Incident`, value the **Dictionary Value**.
     2. **Show Notification**: `Alert sent. Their phone is sounding.` A
        notification, not an alert, so the shortcut carries on without waiting
        for a tap.
     3. **Repeat** 12 times. Inside the repeat:
        1. **Wait** 15 seconds.
        2. **Get Contents of URL**. URL: the Function URL followed by
           `incidents/` and the **Incident** variable. Under **Show More**,
           leave the method as **GET** and add the same `Authorization` header
           as in step 6.
        3. **Get Dictionary Value**: **Value** for key `status` in **Contents
           of URL**. Rename the result `Status`.
        4. **If**: **Status** / **is not** / `active`. Inside it:
           - **Get Dictionary Value**: **Value** for key `summary` in
             **Contents of URL**.
           - **Show Alert**. Title `Emergency Ring`, message the **Dictionary
             Value** just above. Turn **Show Cancel Button** off.
           - **Stop This Shortcut**.

           Delete the **Otherwise** branch.
     4. After **End Repeat**: **Show Alert**. Title `No response yet`, message
        `No acknowledgment after 3 minutes. Their phone keeps alerting. Try
        another way to reach them too.` Turn **Show Cancel Button** off.

     The backend writes the `summary` sentence, for example "Alex answered the
     call and acknowledged." or "Nobody acknowledged. The alerts have stopped;
     keep trying to reach Alex."
   - Inside **Otherwise**:
     1. **Text**: `Emergency from Robin: ` followed by the **Kind** variable,
        then `. Call me now. https://maps.apple.com/?ll=` followed by
        **Current Location** > **Latitude**, a comma, and **Current Location**
        > **Longitude**.
     2. **Run Shortcut**: choose **Emergency Backup**. Tap the arrow and set
        **Input** to the **Text** from the line above.
9. Tap the **(i)** > **Add to Home Screen**. Put it on their first Home Screen
   page, with **Emergency Backup** added the same way beside it.

Do not add either shortcut to the Action Button, Back Tap, a Lock Screen
widget or Control Center. The menu is what stops a pocket press from alerting
the owner's phone. Do not share either shortcut with an iCloud link: the link would
contain the tokens.

### Optional: Emergency Drill

For testing without sending a real emergency, duplicate **Emergency** (long
press > **Duplicate**) and rename the copy **Emergency Drill**. Then change
two things in the copy:

1. In its **Get Contents of URL** for `trigger`, add one more JSON field of
   type **Text**: key `test`, value `drill`. Type it exactly, in lower case.
   The backend treats only `drill`, `full` and JSON true as a test; any other
   value, `Drill` or `true` as text included, starts a real alert.
2. Empty the **Otherwise** branch of step 8, which runs Emergency Backup, and
   put one **Show Alert** there: title `Drill not started`, message `Nothing
   was sent.` Left as it was, a drill that the backend refused, or that met a
   pause, would send a real emergency alert with no TEST label.

Keep it inside the Shortcuts app, not on the Home Screen.

A drill runs the real sequence with the quieter test sound; the alert is
titled "TEST" and the call opens with "This is a test". Use `full` instead of
`drill` for a rehearsal with the real sound.

## Test it, on the sender's phone, with the owner's phone to hand

The first run of each shortcut asks permission for location and for each
website it contacts. Those prompts must be out of the way before a real
emergency.

1. Run **Emergency Backup** from the Home Screen. Tap **Always Allow** when
   asked about `api.pushover.net`. The owner's phone should sound the Pushover
   alert. Acknowledge it in Pushover.
2. Run **Emergency**, choose **Cancel**. Nothing should happen.
3. Run **Emergency Drill** if you have it, otherwise **Emergency**, and
   choose **Kids**. Tap **Always Allow** for location and for the `lambda-url`
   address. Expect the "Alert sent" notification, a Pushover alert on the owner's
   phone with an **Open location** link, and, with the call arm on and the
   default timing, a call about 30 seconds later. Press 1 on the call. Within
   15 seconds the sender's phone should show "Alex answered the call and
   acknowledged."
4. Run it again and this time acknowledge the Pushover alert within 30
   seconds. No call should come, and the sender's phone should show "Alex
   acknowledged the alert." Check that no permission prompt appeared this time.
5. Test the automatic fallback: edit **Emergency**, change `trigger` at the end
   of the URL to `triggerx`, and run it. Expect "Backup alert sent" and a
   Pushover alert titled "EMERGENCY from Robin (backup)" carrying the map
   link. **Change the URL back** and repeat step 3.
6. Turn on Airplane Mode and run **Emergency**. Note how long the error takes
   and what it looks like, so the sender recognises it. Turn Airplane Mode off.
7. Turn Location Services off for Shortcuts and run **Emergency**. If it stops
   with an error instead of sending the alert without a location, leave
   Location Services on and tell the sender that this permission matters.

Repeat steps 1 and 3 after every iOS update, with the owner's phone locked, on
silent and in a Focus.

## If the sender's phone is lost

The shortcuts hold three secrets in plain form: the sender's bearer token, a
Pushover application token and the owner's Pushover user key. (The generated
Emergency Drill holds only the first.)

1. Replace that sender's entry in `trigger-tokens` and make the function read
   it ("Changing one" under [Secrets](deploy.md#secrets)).
2. Replace the Pushover application token the shortcuts carried. If they had
   one of their own, store the new one as `shortcut-pushover-app-token`. If
   it was the backend's own, the backend's `pushover-app-token` secret has to
   change with it, which is the reason to give the shortcuts an application
   of their own.
3. The owner's Pushover user key was on that phone too, and replacing the
   application token does not protect it: anyone who has a user key can send
   that person alerts from an application of their own, emergency-priority
   ones included. We have not checked whether Pushover will issue a new user
   key; ask their support. If the key changes, update `pushover-user-key`,
   any `SenderPushoverKeys` entry that held it, and every Shortcut. If it
   does not, the owner should know that alerts not sent by this system are
   possible.
4. Rebuild the shortcuts for every sender who shared a replaced token or key.

## Notes

- If the sender's phone formats decimals with a comma (some regions do), the
  backend drops the location and still sends the alert. Check that the map
  link in step 3 opens at the right place.
- To let the sender stop a false alarm, the backend has
  `POST /incidents/{id}/cancel` with the same bearer token. It is not wired
  into the shortcut; for now they tell the owner, who acknowledges.
- The status loop holds the shortcut open for up to three minutes. The sender can
  leave it; the alerts on the owner's phone do not depend on it.
