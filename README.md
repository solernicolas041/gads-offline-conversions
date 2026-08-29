# gads-offline-conversions

**Send the real revenue back to Google Ads, from a CSV.**

```bash
python gads_offline.py --customer-id 123-456-7890 \
    --conversion-action 987654321 --mode calls --csv won_jobs.csv
```

Dry run by default. Nothing is uploaded until you add `--live`.

---

## Why this exists

Smart Bidding optimises toward whatever you tell it a conversion is worth. Most
lead-gen accounts tell it "a form submit", which is worth the same €0 whether the
lead closed at €4,800 or never answered the phone. The algorithm then does exactly
what it was asked: it buys more form submits.

Sending closed revenue back closes that loop. It is also the step most agencies
skip, because the API has three different upload paths and picking the wrong one
fails **silently**.

That silent failure is what this tool exists to prevent.

---

## The mistake this refuses to let you make

A phone lead uploaded as a *click* conversion onto an `UPLOAD_CALLS` action is
accepted by the API — HTTP 200, no error, no warning — and recorded as nothing at
all. You find out months later, when the account still shows zero offline
conversions.

`gads_offline.py` reads the conversion action back from the API before uploading
and refuses to run when the mode and the action type disagree:

```
ERROR: --mode clicks writes to a UPLOAD_CLICKS action, but 'Paid job (revenue)' is UPLOAD_CALLS.
Uploading anyway is accepted by the API and records nothing.
```

It also refuses to upload to an action that is not `ENABLED`, for the same reason:
those uploads are discarded.

---

## Three modes, one per way the lead arrived

| Mode | Use when | Matches on | Action type required |
|---|---|---|---|
| `calls` | The lead called a Google forwarding number | `caller_id` + exact call start time | `UPLOAD_CALLS` |
| `clicks` | You stored the `gclid` / `wbraid` / `gbraid` at capture | the click id | `UPLOAD_CLICKS` |
| `enhanced` | No click id at all — CRM export only | SHA-256 hashed email and/or phone | `UPLOAD_CLICKS` |

`enhanced` is Enhanced Conversions for Leads: Google matches the hashed identifier
back to the signed-in user who clicked. It recovers conversions that a lost `gclid`
would have thrown away.

---

## CSV format

One row per closed deal. Column names matter; extra columns are ignored.

**`--mode calls`** — see `examples/calls.csv`

```csv
label,caller_id,call_start_time,conversion_time,value,currency
Windscreen replacement - Peugeot 208,0639987601,2026-07-14 15:09:11,2026-07-14 18:00:00,450,EUR
```

`call_start_time` must be the **exact** start of the call, to the second, as Google
recorded it. Query it from `call_view` if your CRM only stores the minute — a call
conversion whose timestamp does not match a real call is rejected with
`CALL_NOT_FOUND`, and that revenue never lands.

**`--mode clicks`** — see `examples/clicks.csv`

```csv
label,gclid,conversion_time,value,currency
Deal 4412 - annual contract,Cj0KCQjw_ExampleGclid_0001,2026-07-14 10:12:00,4800,EUR
```

**`--mode enhanced`** — see `examples/enhanced.csv`

```csv
label,email,phone,conversion_time,value,currency
Lead 881 - closed won,jane.doe@example.com,0639987601,2026-07-14 10:12:00,2400,EUR
```

Either email or phone is enough; both is better. They are hashed locally before
anything leaves your machine — raw identifiers are never sent.

---

## What it does to your data before uploading

- **Timestamps get the account's UTC offset**, read from the API — never guessed.
  A naive timestamp is read as UTC, which files a summer afternoon conversion on a
  Paris account two hours early, sometimes into the wrong day.
- **Phone numbers are normalised to E.164**, including the two ways spreadsheets
  break them: the trailing `.0` of a number stored as a float, and the dropped
  leading zero. Pass `--default-country 33` for national numbers.
- **Emails are lowercased and trimmed**, with Gmail dots and `+tags` removed, then
  SHA-256 hashed.
- **Amounts accept both decimal separators** — `1 234,56 €` and `$1,234.56` both
  parse to `1234.56`.
- **Duplicates are dropped.** A CRM export with one row per interaction repeats the
  same closed deal; each copy is a real conversion to Google, and the account's
  revenue silently inflates. Rows sharing an identifier *and* a timestamp are
  counted once, and the dry run tells you how much value that removed. Override
  with `--keep-duplicates` if you really mean it.
- **A bad row never stops the file.** It is skipped, counted, and reported with the
  reason and its line number.

---

## Dry run first, always

```
# Offline conversions — DRY RUN

Action   : Paid job (revenue) (UPLOAD_CALLS, ENABLED)
Mode     : calls
Uploading: 3 conversions, 1,520.00 EUR

| Line | Label | Value | Conversion time | Matched on |
|---|---|---|---|---|
| 2 | Windscreen replacement - Peugeot 208 | 450.00 | 2026-07-14 18:00:00 | +33639987601 at 15:09:11 |
| 3 | ADAS recalibration - Renault Clio | 780.00 | 2026-07-15 17:30:00 | +33639980042 at 09:41:02 |

## Duplicates dropped: 1 (450.00 EUR)
Same identifier and same timestamp as a row already counted.

## Skipped: 2 rows
- 2x caller_id is missing or unreadable

Nothing was sent. Re-run with --live to upload.
```

Add `--live` and the same report prints, followed by what Google accepted:

```
Uploaded 17/19 conversions to 'Paid job (revenue)'.

2 row(s) rejected by Google:
  line 8 (Job 4419, 320.0): CallError.CALL_NOT_FOUND
  line 14 (Job 4431, 180.0): CollectionSizeError.TOO_LOW

That revenue did NOT land. Fix these rows and re-run.
```

Partial failure is enabled, so a bad row never blocks the good ones — and every
rejected row is mapped back to **your** CSV line, not to an opaque index. Google
deduplicates an already-accepted conversion, so re-running after a fix is safe.

---

## Install

```bash
git clone https://github.com/solernicolas041/gads-offline-conversions.git
cd gads-offline-conversions
pip install -r requirements.txt      # google-ads only
```

You need a **developer token** (Ads UI → Tools → API Center), an **OAuth client**
(Cloud Console → Credentials → Desktop app), and a **refresh token**:

```bash
python generate_refresh_token.py
```

Put all four in `google-ads.yaml` (copy `google-ads.yaml.example`). That file is
git-ignored and read only by Google's own client library.

---

## Run it on a schedule

```cron
# every Monday 8am — push last week's closed deals
0 8 * * 1  cd /path/to/gads-offline-conversions && \
           python gads_offline.py --customer-id 123-456-7890 \
             --conversion-action 987654321 --mode enhanced \
             --csv /path/to/last_week_won.csv --live >> upload.log 2>&1
```

Export from your CRM into that CSV however you like — this tool deliberately knows
nothing about your CRM, so it works with all of them.

---

## Tests

```bash
python3 tests/test_gads_offline.py
```

35 tests, no credentials and no network: identifier normalisation, timestamp and
amount parsing, per-row skip reasons, deduplication, and the mode/action-type guard
in both directions.

---

## Requirements

- Python 3.9+
- `google-ads` (only dependency)
- A conversion action of the right type, already created in the account
- Google Ads API access at Basic level or above

---

## FAQ

**Will this double-count if I re-run it?**
Google deduplicates an identical conversion (same identifier, same timestamp, same
action). The tool also drops repeats inside a single file before uploading.

**How far back can I upload?**
90 days for click conversions, and the conversion action's click-through window
still applies. Older rows are rejected, and you will see which ones.

**Does it send my customers' email addresses to Google?**
Only as SHA-256 hashes, computed on your machine. Raw values never leave it.

**Can it write anything else to my account?**
No. The only write path in this repo is the conversion upload itself.

---

## License

MIT.
