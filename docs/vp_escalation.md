# VP+ escalation (meetingbuddy) — operator notes

`vp_escalation.py` brings an exec (Aman/Zac) into VP+ ICP demos. **Off by default.**
Reads the canonical ICP from `FAI/icp/loader.py` — no local ICP rules.

## Wiring into `meeting_bot.py`

After a booking is written to HubSpot, call the pure orchestrator and execute its
returned action (it performs no I/O itself):

```python
import vp_escalation as vp
busy = {"aman": vp.freebusy("aman", start, end) or 0,
        "zac":  vp.freebusy("zac",  start, end) or 0}
action = vp.handle_booked_meeting(meeting, contact, busy)
# action["action"] in {none, digest, propose, auto_add}
#   propose  -> say(blocks=action["blocks"], thread_ts=ts) and handle button clicks
#   digest   -> append to the exec feed channel
#   auto_add -> already ran add_guest() (a no-op while DRYRUN=1)
```

## Env flags

| Var | Default | Meaning |
|---|---|---|
| `VP_ESCALATION_ENABLED` | `0` | Master switch. Nothing happens while `0`. |
| `VP_ESCALATION_DRYRUN` | `1` | `1` = log intent, no calendar write / no prospect email. Set `0` only after a clean dry-run day. |
| `VP_ESCALATION_MODE` | `auto` | `digest` \| `propose` \| `auto`. **Default `auto`**: add the freer exec (with `sendUpdates=none`, so the prospect isn't emailed) AND post a flag in the meeting thread. `propose` = button-confirm instead. |
| `GOOGLE_CALENDAR_TOKEN_JSON` | — | **NEW** credential (Option A: service-account JSON with domain-wide delegation; or Option B: user OAuth token). Until set, `freebusy`/`add_guest` are guarded no-ops and selection falls back to round-robin. |
| `VP_EXEC_CAL_AMAN` / `VP_EXEC_CAL_ZAC` | `<name>@furtherai.com` | Exec calendar addresses (impersonated for free/busy). |
| `VP_ORGANIZER_CALENDAR_ID` | `primary` | The AE/organizer calendar an event lives on. Prefer an @furtherai.com address so the service account impersonates that organizer to patch the event. |
| `VP_CALENDAR_SUBJECT` | — | Fallback impersonation subject when the organizer calendar isn't an email (e.g. `primary`). |

**Option A (service account + domain-wide delegation) — the identity model in use:**
- Enable **Google Calendar API**; create a service account with **domain-wide delegation**.
- In Workspace Admin → API controls → domain-wide delegation, authorize the SA's client ID for scopes `calendar.events` + `calendar.freebusy`.
- Put the SA JSON key in `GOOGLE_CALENDAR_TOKEN_JSON`. The bot impersonates the exec for free/busy and the organizer for the guest-add — no per-user calendar sharing needed.

## Go-live order (owner)

1. Grant Google Calendar OAuth scope; set `GOOGLE_CALENDAR_TOKEN_JSON`.
2. `VP_ESCALATION_ENABLED=1`, keep `DRYRUN=1` (default `MODE=auto`). Watch a day of
   logs — confirm it fires on the right meetings and the freer-exec pick looks right.
3. Flip `DRYRUN=0` to go live. The exec is auto-added with `sendUpdates=none`, so the
   prospect gets **no** attendee-change email; the exec sees it on their calendar and
   the bot posts a flag in the meeting thread (cc Aman/Zac to swap).

## Guarantees

- `is_escalation_candidate` requires full ICP (segment+size+seniority+function) and a
  `demo` type; conference touches are skipped.
- Idempotent: an exec already on the invite, or a `vp_escalated_exec` marker, blocks
  re-entry — no duplicate invite emails.
- `add_guest` refuses to act unless `ENABLED=1` and `DRYRUN=0` and a real GCal cred
  exists — three independent gates before a prospect can ever be emailed.
