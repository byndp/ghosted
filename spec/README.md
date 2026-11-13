# Push-rule vectors

`push-rules-vectors.json` is the shared truth for the push rules in `docs/contract.md` section 12.
Two engines must produce the expectation of every vector they run:

- **Python**: `core.schedule.decide`, the reference used by the local adapters (ntfy, Pushover),
  tested by `tests/test_schedule.py`.
- **JS**: a port for a self-hosted phone server (`--adapter remote`). That server is not in this
  repo yet, so the vectors marked `["js"]` only are not run here.

Every expectation was derived by hand from the contract before any engine ran it, and each
vector's `why` records that derivation. When an engine disagrees, the contract text decides (see
"Adding or changing a vector").

## File format

```json
{
  "version": 1,
  "vectors": [
    {
      "name": "due-today-T1",
      "why": "09:15. GS-0001 is open and due today (T1), so the digest goes out with it. ...",
      "now": "2026-10-06T14:15:00Z",
      "tz": "America/Chicago",
      "cfg": {"digest_hour": 9, "quiet_start": 22, "quiet_end": 8, "daily_budget": 3,
              "deadman_hours": 3, "enable_deadman": false},
      "engines": ["py", "js"],
      "items": [{"id": "GS-0001", "kind": "promise", "project": "shop", "title": "shop: ...",
                 "owner": "me", "status": "open", "due_at": "2026-10-06", "review_at": null,
                 "snooze_until": null, "created_at": "2026-10-01T15:00:00Z"}],
      "notifications": [{"id": 1, "sent_at": "2026-10-05T14:15:30Z", "kind": "digest",
                         "dedup_key": "digest:2026-10-05", "item_ids": ["GS-0090"], "ok": 1,
                         "local_date": "2026-10-05"}],
      "runs": [{"run_id": "run-hb", "started_at": "2026-10-06T14:01:00Z",
                "finished_at": "2026-10-06T14:03:00Z", "ok": 1, "sessions_scanned": 3,
                "items_added": 0, "error": null}],
      "expect": {"reopen": [],
                 "send": [{"kind": "digest", "dedup_key": "digest:2026-10-06", "item_ids": ["GS-0001"]}]}
    }
  ]
}
```

| field | meaning |
|---|---|
| `name` | unique, kebab-case; says what the vector pins |
| `why` | the hand derivation; informative, harnesses ignore it |
| `now` | the tick, an ISO-8601 instant (`Z` or an offset, optional fractional seconds) |
| `tz` | IANA zone the rules run in; every vector uses `America/Chicago` |
| `cfg` | all six keys, always present |
| `engines` | `["py", "js"]`: both suites run it. `["js"]`: a dead-man vector (Worker only) |
| `items` | contract section 4 shape. The rules read `id, kind, title, status, due_at, review_at, snooze_until, created_at, looks_resolved`; the rest is realistic filler. Projects and titles are invented. A missing `looks_resolved` reads as false |
| `notifications` | rows as the `notifications` table / `notifications.jsonl` holds them. `item_ids` is an array, or the JSON text of one (the D1 column) |
| `runs` | rows as the `runs` table holds them (`ok` is `1`/`0`; `true` also counts as ok) |
| `expect.reopen` | ids to reopen, in ascending id order |
| `expect.send` | pushes in send order, each reduced to `kind, dedup_key, item_ids` |
| `expect_bodies` | optional; the `body` of each push, aligned with `expect.send` |

## Running a vector

```
result = decide(now, items, notifications, runs, tz, cfg)        # JS: decide({now, items, notifications, runs, tz, cfg})
result.reopen                                  == expect.reopen
[{kind, dedup_key, item_ids} of result.send]   == expect.send        (order matters)
[body of result.send]                          == expect_bodies       (only when present)
```

- **Order matters** in `send`: it is the send order, and a digest's `item_ids` is its body order.
  Where more than one id reopens, the vector also lists those items in id order, so an engine that
  keeps input order still matches `reopen`.
- **Dead-man vectors** (`["js"]`) have `enable_deadman: true`. The contract says Python skips them;
  `tests/test_schedule.py` skips them in its vector runner and then runs them a second way, through
  Python's own dead-man path, so the reference and the port answer to the same cases.
- **Shared vectors are dead-man-neutral.** Each one carries a heartbeat (an ok run that finished
  12 minutes before `now`) or sits outside the dead-man window, so its expectation is the same
  whether or not the engine runs the dead-man. Both suites assert this by running every shared
  vector again with `enable_deadman` forced on, which is how the Worker always runs.

## The rules, as the vectors pin them

Section 12 of the contract, with every ambiguity resolved. Vector names in brackets.

**Time.** `now`, `sent_at`, `finished_at` and `created_at` are instants; a naive one is UTC.
`due_at` and `snooze_until` are local wall times in `tz`: `YYYY-MM-DD` (date-only) or
`YYYY-MM-DDTHH:MM`. A wall time is turned into an instant in `tz`; a date-only value means local
00:00. On DST days this follows PEP 495 `fold=0`, as `core.timeutil` does: an ambiguous fall-back
time means its earlier instant (01:30 on 2026-11-01 is 06:30Z), and a time inside the
spring-forward gap maps on the pre-transition offset (02:30 on 2026-03-08 is 08:30Z, the same
instant JS Temporal's `"compatible"` disambiguation gives) [`dst-gap-*`, `dst-fall-back-*`].
`review_at` and `local_date` are local dates. Local hour and "today" come from `now` in `tz`.
Instants are compared on the UTC timeline, so "24 h" and "48 h" are elapsed hours, not wall-clock
days [`dst-advance-*`]. The engines must follow real DST [`dst-*`].

1. **Reopen.** Every `snoozed` item whose `snooze_until` <= now is listed, in id order, on every
   tick, quiet hours included [`quiet-hours-*`]. The caller sets it open (rev + 1) and **keeps
   `snooze_until`**: snooze expiry leaves it in place so T5 still sees the item as reopened today
   at later ticks; only a manual reopen (the phone's Undo, `ghosted` reopen) clears it. Within the
   tick a reopened item already counts as open for every rule below
   [`advance-for-an-item-reopened-this-tick`].
2. **Quiet hours.** Local hour >= `quiet_start` or < `quiet_end` (22 and 8): nothing is sent.
3. **Active items** = status `open`, plus the items reopened in this tick. `done` and `dismissed`
   items never appear [`closed-items-never-appear`]. A snoozed item that is not reopened is not a
   digest item and gets no advance [`snoozed-*`].
4. **Budget.** Rows whose `local_date` is today count, of any kind and any `ok`; the local date,
   not the UTC date [`budget-counts-local-date-not-utc-date`]. Pushes decided earlier in the same
   tick count too. At most `daily_budget` per local day, dead-man included.
5. **Dedup.** A row with the push's `dedup_key` blocks it, whatever its `ok` says: the caller inserts
   the row before sending, so a failed send is not retried [`failed-digest-row-still-dedups`].
6. **Order inside a tick:** dead-man, digest, then advance pushes, earliest due first (ties by id).
7. **Digest.** Local hour in [`digest_hour`, `digest_hour` + 3), no `digest:<today>` row, budget
   left, and at least one active item matching a trigger. Key `digest:YYYY-MM-DD`.
   - **T2 overdue**: date-only due before today; timed due instant before now, even earlier today
     [`timed-item-past-its-time-today-sorts-as-overdue`].
   - **T1 due today**: the date part of `due_at` is today.
   - **T3 due soon**: date-only due on today+1 or today+2; timed due within the next 48 h, bound
     included (due - now <= 48 h), whatever its date [`timed-T3-*`].
   - **T4 new interruption**: kind `interrupted`, not `looks_resolved`, and `created_at` strictly
     after the `sent_at` of the latest *delivered* digest: the digest row with the latest
     `sent_at` among those with `ok` 1 or true (other kinds do not count). A failed send (`ok` 0)
     reached nobody, so the interruptions it carried are still new and go into the next digest;
     the failed row still dedups its own day and counts in that day's budget. When no delivered
     digest row exists (none at all, or only failed ones), every active interrupted item that is
     not `looks_resolved` [`first-ever-*`, `t4-measures-from-the-latest-delivered-digest`,
     `t4-no-delivered-digest-counts-as-never`,
     `interruption-created-at-the-digest-instant-is-not-new`]. `looks_resolved` (the session was
     resumed and ended clean) counts only when it is `true` or `1`, the way D1 returns booleans;
     `false`, `0`, null and a missing field do not. It takes the item out of T4 only: a resolved
     interruption still meets T5 and T6 like any item [`t4-skips-interruptions-that-look-resolved`,
     `t4-first-ever-digest-skips-interruptions-that-look-resolved`].
   - **T5 reopened today**: reopened in this tick, or open with `snooze_until` dated today and
     <= now [`reopened-earlier-today-counts-T5`]. The second branch is the common one in real data:
     the item was reopened at an earlier tick today (often a quiet-hours one), or by the Worker's
     cron, which reopens due snoozes in D1 just before it calls `decide` (contract 15), so `decide`
     sees it open with its `snooze_until` kept
     [`reopened-at-the-0730-quiet-tick-drives-the-0915-digest-T5`,
     `cron-reopened-before-decide-T5-both-branches-sort-by-due`]. An open item with a null
     `snooze_until` (reopened by hand) is not T5.
   - **T6 review**: `review_at` <= today, and (today - `review_at`) is a whole number of weeks,
     whether or not the item also has a `due_at` (`ghosted add --due ... --review ...` stores both)
     [`review-at-*`].
   - Owner and kind do not filter, apart from T4 [`owner-claude-items-count`].

   The digest carries every active item that matches any trigger. Each item sits in the first
   trigger it matches in the order T2, T1, T3, T4, T5, T6; inside a trigger, by due instant (items
   without a due last), then by id [`ordering-T2-T1-T3-T4-T5-T6-then-by-due`].

   Body: `"{n} open: {title} · {title} · …"`, where `n` counts every digest item and titles are the
   public titles, verbatim (the project prefix stays). When the whole body would exceed 178
   characters, it keeps as many whole titles as fit and appends `" · …"`, and the limit includes
   that mark [`digest-body-*`]. Characters are Unicode code points (Python `len`; in JS
   `Array.from(s).length`, never `s.length`, which counts UTF-16 units and makes an emoji 2)
   [`digest-body-counts-code-points-not-utf16-units`]. A title that is missing or blank (only
   ASCII whitespace) shows as the item id.
8. **Advance.** An active item whose `due_at` has a time, with due - 24 h <= now < due, and no
   `advance:<id>` row: one push. There is one per item, ever, even after the due changes
   [`advance-once-per-item-ever`]. Title `Due in 24 h`, body = the item's public title. Date-only
   items never get one; T3 is their warning. Advance has no window of its own: any hour outside
   quiet hours [`advance-fires-at-0815-*`, `advance-fires-at-2115-*`].

   **No double push for one item.** The advance is skipped when a *delivered* digest (kind digest,
   `ok` 1 or true, readable `sent_at`) whose `item_ids` include the item was sent at or after
   due - 24 h, from any day [`advance-skipped-after-a-delivered-digest-carried-it`,
   `advance-skip-counts-a-digest-sent-exactly-at-due-minus-24h`,
   `advance-stays-skipped-the-next-morning`], or when the digest decided in this same tick
   includes it [`digest-then-advance-in-the-same-tick`]. A failed digest send reached nobody and
   does not count [`advance-fires-when-the-digest-that-carried-it-failed`], nor does a delivered
   digest that did not list the item [`advance-fires-when-the-delivered-digest-did-not-carry-it`]
   or one sent before due - 24 h (the earlier vectors' 09:15 digests). A skipped advance uses no
   budget, and its `advance:<id>` key stays free. Every item inside its advance window is due
   within 24 h, so it always meets T1 or T3: **a tick that sends the digest never sends an
   advance**. `item_ids` may be the D1 column's JSON text; it is read as the array it holds, and
   anything that is not an array of ids (bad JSON, `NaN`, an object) lists nothing
   [`advance-skip-reads-item-ids-stored-as-json-text`].
9. **Dead-man** (the Worker; Python only with `enable_deadman`). Local hour in [`digest_hour`,
   `quiet_start`). The last ok run (the latest `finished_at` among rows with `ok` 1 or true) finished
   more than `deadman_hours` ago, or there is no ok run at all. And no row exists with key
   `deadman:<that run_id>` (`deadman:none` when there is no ok run). Then one push with
   `item_ids: []` and priority `high`. It cannot repeat until a new ok run changes the key
   [`deadman-*`].

**Push shape.** Every push is `{kind, dedup_key, item_ids, title, body, local_date, priority}` with
`local_date` = today and priority `high` for the dead-man, else `normal`. The vectors check `kind`,
`dedup_key` and `item_ids`, plus `body` where `expect_bodies` is given. The contract fixes the
titles (`Ghosted`, `Due in 24 h`, `Mac stopped reporting`) and the dead-man body
(`No successful run since 8:03 AM`; `... since Mon Oct 5, 11:02 PM` when the run was on another
local day; `No successful run reported yet` when there is none). The vectors leave them to the
unit tests and the cross-engine fuzz, which compare every field.

## Input handling (both engines)

The JS port reproduces all of this. `tests/test_schedule.py` and `rules.node.test.mjs` unit-test it,
and the port was checked against the reference with a seeded cross-engine fuzz (whole pushes
compared, titles and bodies included; see "The JS port").

- Text is read as ASCII: digits are `0-9` only, and the only whitespace stripped from values is
  ASCII (space, tab, LF, CR, VT, FF). A due date written in other digits or wrapped in a no-break
  space is unreadable, in both engines alike.
- `quiet_start < quiet_end` is read as a same-day window [start, end), and equal bounds mean no quiet
  hours. For the default 22/8, and any start > end, this matches the contract formula exactly.
- A notification row without `dedup_key` still dedups: digests by `kind` + local date, advances by
  their single item id. A row without `kind` takes it from its `dedup_key` prefix. A row without
  `local_date` counts on the local date of its `sent_at`.
- `cfg` may be a dict (JS: an object) or an object with attributes; missing or null keys take the
  defaults above. The four hour/budget keys take an integer, a finite number (truncated) or a
  decimal string such as `"9"` or `" +9 "`; `deadman_hours` takes a finite number of hours or a
  decimal string (`"2.5"`, `".5"`, `"2e0"`). Anything else (`"9_0"`, `"0x10"`, `NaN`, a list)
  raises. `enable_deadman` accepts `"1"`, `"true"`, `"yes"`, `"on"` in any case as true, any other
  string as false, and other values by Python truthiness (an empty list or object is false).
- Item ids are non-empty strings; a row whose `id` is anything else is skipped. With items given as
  a dict (JS: an object) of id -> item, the key stands in for a missing, null or empty `id`. The
  first row with a given id wins. Rows that are not objects are skipped, and an `items`,
  `notifications` or `runs` value that is not a list holds no rows.
- A run's id is its `run_id` (a non-empty string, or an integer), else its `id` (D1's row id), else
  `unknown`.
- Unparseable times are ignored: no trigger, no crash. So are times at the edge of the datetime
  range that overflow when shifted between zones (`9999-12-31T23:00` in Chicago, year 1 east of
  UTC); one such value never stops the other pushes. Instants keep microseconds (more fraction
  digits are truncated); an offset must be under 24 h. `decide` raises for a bad `now` (or one
  outside years 0002..9998), `tz`, or `cfg` value. It never mutates its inputs.
- A digest row counts as delivered (for T4 and the no-double-push rule) only with `ok` 1 or true and
  a readable `sent_at`.

## The JS port

`rules.js` mirrors `core/schedule.py` function by function; `time.js` holds the Intl helpers and
mirrors `core/timeutil.py` (`wallToUtc`, `snoozeUntil`, `toIso`, `parseIso`, `humanTime`,
`humanDate`). Points where JS needs care, all handled there:

- Instants are BigInt microseconds since the epoch, so fractional seconds compare exactly as
  Python's `datetime` does (a JS `Date` keeps only milliseconds). `deadman_hours` becomes
  microseconds the way `timedelta(hours=x)` rounds.
- Zone offsets come from `Intl.DateTimeFormat#formatToParts` (`hourCycle: "h23"`). A wall time maps
  to an instant by probing the offsets a day either side: when both candidates are valid (fall-back)
  the earlier instant wins, and when neither is (the spring-forward gap) the pre-transition offset
  is used. That is `fold=0`, matching zoneinfo for any zone with at most one transition within
  +/- 24 h. Offset zone names such as `+05:00`, which Intl accepts and zoneinfo does not, are
  refused.
- Strings sort by code point, as Python does, not by UTF-16 unit.
- `decide` takes one object: `decide({now, items, notifications, runs, tz, cfg})`; `now` is a
  `Date` or an ISO string. The Worker passes `enable_deadman: true`.

Cross-engine check (2026-10-05, a one-off script, not kept in the repo): a seeded generator made
random scenarios across ten zones and their DST days (odd ISO spellings, JSON-text `item_ids`,
failed and boundary digests, bad config), recorded `core.schedule.decide`'s full answer or its
error, and `rules.js` matched every one: 300/300 on the main seed, 3000/3000 over ten more seeds
and 4000/4000 on a large batch, where the no-double-push rule decided 212 scenarios,
`looks_resolved` 33 and T5's second branch 120. `time.js` matched `core.timeutil` on 800 random
wall times, 300 snoozes, 300 ISO strings and 200 local renderings.

Known differences, outside anything the contract can produce: Python reads JSON `7.0` as a float
and JS cannot tell it from `7`, so a float run id is `unknown` in Python and `7` in JS; integers
beyond 2^53 lose precision in JS; zone names that only one of zoneinfo and ICU knows.

## Adding or changing a vector

1. Derive the expectation by hand from `docs/contract.md` section 12 and the rules above, and write
   the derivation into `why`. Never generate an expectation by running an engine.
2. Keep shared vectors dead-man-neutral (add a fresh ok run). A dead-man vector gets
   `"engines": ["js"]` and `"enable_deadman": true`.
3. Use invented projects and neutral public titles: 1 to 100 characters, accepted by
   `core.sanitize` (tested), and interrupted titles in the template
   `"{project}: session MM-DD ended on usage limit|an open question"`. Nothing from a real session
   transcript goes in this file, and no real project or obligation either, not even paraphrased:
   the core is an open-source candidate.
4. Run both suites. When an engine disagrees, re-derive from the contract text and fix whichever side
   is wrong. If the contract itself is ambiguous, resolve it in this file (and the contract) first.

Most vectors are set on Tue 2026-10-06, when Chicago is on CDT (UTC-5): local 09:15 is 14:15Z.
The DST vectors use Sun 2026-03-08 (the spring-forward gap, 08:00Z), Fri 10-30 (CDT), Sat 10-31,
Sun 11-01 (the fall-back, 07:00Z) and Mon 11-02 (CST, UTC-6). The gap and fold vectors run in
quiet hours and test `reopen`, which is decided on every tick.

### What the callers pass as `created_at` (review fixes, 2026-10-05)

`decide`, both engines and every vector are unchanged. T4 reads an item's `created_at`; the callers
now pass an interrupted item's T4 time there:

- the Worker's cron (`db.ruleItem`): the latest of `created_at`, `interrupted_at` (when its newest
  flagged ending happened) and `first_seen_at` (when the server first saw the current
  interruption: its insert, or an upsert that reopened or un-resolved it). So an interruption the
  Mac found before a digest but uploaded after it (a failed sync), or one reopened after Done
  (contract 6), is still "new since the last digest".
- `core.runner.local_push` (`t4_items`): the later of `created_at` and `reopened_at`.

Two caller-side rules sit around `decide` and leave its answers alone: the Worker inserts no
notification row when no channel can deliver (no subscription, no VAPID secrets, no Pushover), and
the local adapters claim nothing when their server cannot be reached. In both cases the push is
decided again at the next tick. The Worker also claims each cron tick, so a second invocation of
one tick sends nothing.

### Changes after stage 1 (contract 12, 2026-10-05)

- `digest-then-advance-in-the-same-tick`: expectation changed. The digest decided in the tick carries
  GS-0001, so its advance is no longer sent (no double push); the name is kept for traceability.
- `budget-digest-takes-the-last-slot-advance-waits` renamed `budget-digest-takes-the-last-slot`:
  same expectation, but the advance no longer "waits" for a later tick (once a delivered digest
  carried it after due - 24 h, no tick sends it).
- Derivation notes added, expectations unchanged: `advance-at-due-minus-24h`,
  `advance-window-start-is-inclusive`, `advance-fires-at-2115-the-last-hour-before-quiet`,
  `advance-for-an-item-reopened-this-tick`, `advances-ordered-by-due-and-cut-by-budget` and
  `dst-advance-is-24-elapsed-hours-fires` (each item's 09:15 digest went out before due - 24 h).
- Added: the two `t4-*-look-resolved` vectors, the two T5 vectors named above, and six
  no-double-push vectors (`advance-skip*`, `advance-fires-when-*`, `advance-stays-*`).
