#!/usr/bin/env python3
"""gads_offline.py — send real revenue back to Google Ads from a CSV.

Three upload modes, because the channel a lead came through decides the API call:

    clicks     a gclid / wbraid / gbraid you stored at lead capture
    calls      a phone lead from a Google forwarding number (caller id + call time)
    enhanced   no click id at all — match on hashed email / phone (ECL)

    python gads_offline.py --customer-id 123-456-7890 \
        --conversion-action 987654321 --mode calls --csv won_jobs.csv

Dry run by default. `--live` actually uploads.

The mode has to match how the conversion action was created. A phone lead sent as
a click conversion onto an UPLOAD_CALLS action is accepted by the API and then
recorded as nothing at all — the failure is silent, and it is the single most
expensive mistake in offline conversion work. This tool refuses to upload when the
mode and the action type disagree.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

YAML_PATH = Path(__file__).parent / "google-ads.yaml"

# Which conversion-action types each mode is allowed to write to.
MODE_ACTION_TYPES = {
    "clicks": {"UPLOAD_CLICKS"},
    "enhanced": {"UPLOAD_CLICKS"},
    "calls": {"UPLOAD_CALLS"},
}

CLICK_ID_FIELDS = ("gclid", "wbraid", "gbraid")
REQUIRED_COLUMNS = {
    "clicks": {"conversion_time", "value"},
    "calls": {"caller_id", "call_start_time", "conversion_time", "value"},
    "enhanced": {"conversion_time", "value"},
}

TZ_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}$")
DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Normalisation — Google matches on exact bytes, so this is where leads are won
# ---------------------------------------------------------------------------
def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def norm_email(raw: str | None) -> str | None:
    """Lowercase, trim, and strip gmail dots/plus-tags before hashing."""
    email = (raw or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return None
    local, domain = email.rsplit("@", 1)
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if local else None


def norm_phone(raw: str | None, default_country: str = "") -> str | None:
    """Return an E.164 phone, or None. `default_country` is a dial code like '33'.

    Spreadsheets mangle phone numbers into floats ('639987601.0') and drop the
    leading zero; both cases are handled here rather than in every caller.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    keep_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if keep_plus:
        return f"+{digits}"
    if default_country:
        national = digits[1:] if digits.startswith("0") else digits
        return f"+{default_country}{national}"
    return f"+{digits}"


def parse_datetime(raw: str, tz_offset: str) -> str:
    """'2026-07-14 15:09:11' -> '2026-07-14 15:09:11+02:00'.

    Google reads the timestamp in the ACCOUNT's time zone. A naive string is
    interpreted as UTC, which on a Europe/Paris account files every summer
    conversion two hours early — enough to land it in the wrong day, and for a
    call conversion, enough to miss the call entirely.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if TZ_OFFSET_RE.search(text):
        return text
    for fmt in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d %H:%M:%S") + tz_offset
    raise ValueError(f"unreadable timestamp {raw!r} (expected YYYY-MM-DD HH:MM:SS)")


def parse_amount(raw: str) -> float:
    """'1 234,56 €' -> 1234.56. Accepts both decimal separators."""
    text = re.sub(r"[^\d,.\-]", "", (raw or "").strip())
    if not text:
        raise ValueError("empty amount")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") \
            else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    return float(text)


# ---------------------------------------------------------------------------
# CSV -> rows
# ---------------------------------------------------------------------------
def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(fh)]


def build_rows(raw_rows: list[dict], mode: str, tz_offset: str,
               currency: str, default_country: str = "") -> tuple[list[dict], list[dict]]:
    """Return (uploadable, skipped). A skipped row always carries its reason."""
    ok: list[dict] = []
    skipped: list[dict] = []
    for i, r in enumerate(raw_rows, start=2):  # header is line 1
        try:
            row = {
                "line": i,
                "label": r.get("label") or r.get("name") or f"line {i}",
                "value": parse_amount(r.get("value", "")),
                "currency": (r.get("currency") or currency).upper(),
                "conversion_time": parse_datetime(r.get("conversion_time", ""), tz_offset),
            }
            if mode == "clicks":
                click_id = next((r[f] for f in CLICK_ID_FIELDS if r.get(f)), "")
                if not click_id:
                    raise ValueError("no gclid / wbraid / gbraid on this row")
                field = next(f for f in CLICK_ID_FIELDS if r.get(f))
                row["click_id_field"], row["click_id"] = field, click_id
            elif mode == "calls":
                caller = norm_phone(r.get("caller_id"), default_country)
                if not caller:
                    raise ValueError("caller_id is missing or unreadable")
                row["caller_id"] = caller
                row["call_start_time"] = parse_datetime(r.get("call_start_time", ""), tz_offset)
            else:  # enhanced
                email, phone = norm_email(r.get("email")), norm_phone(r.get("phone"), default_country)
                if not email and not phone:
                    raise ValueError("neither a usable email nor a usable phone")
                row["hashed_email"] = sha256(email) if email else None
                row["hashed_phone"] = sha256(phone) if phone else None
            ok.append(row)
        except Exception as exc:  # noqa: BLE001 — one bad line never stops the file
            skipped.append({"line": i, "label": r.get("label") or f"line {i}", "reason": str(exc)})
    return ok, skipped


def dedupe(rows: list[dict], mode: str) -> tuple[list[dict], list[dict]]:
    """Drop rows that repeat the same conversion. Returns (kept, dropped).

    A CRM export with one line per interaction sends the same closed deal several
    times; each copy is a real conversion as far as Google is concerned, and the
    account's revenue silently inflates. Identity is the pair Google itself would
    match on, plus the timestamp.
    """
    def key(r: dict) -> tuple:
        if mode == "calls":
            return ("call", r["caller_id"], r["call_start_time"])
        if mode == "clicks":
            return ("click", r["click_id"], r["conversion_time"])
        return ("ecl", r.get("hashed_email"), r.get("hashed_phone"), r["conversion_time"])

    seen: set = set()
    kept, dropped = [], []
    for r in rows:
        k = key(r)
        (dropped if k in seen else kept).append(r)
        seen.add(k)
    return kept, dropped


# ---------------------------------------------------------------------------
# Google Ads
# ---------------------------------------------------------------------------
def get_client():
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        sys.exit("ERROR: google-ads is not installed. Run: pip install -r requirements.txt")
    if not YAML_PATH.exists():
        sys.exit(f"ERROR: {YAML_PATH.name} not found. Copy google-ads.yaml.example and fill it in.")
    return GoogleAdsClient.load_from_storage(str(YAML_PATH))


def query(client, customer_id: str, gaql: str) -> list:
    service = client.get_service("GoogleAdsService")
    rows = []
    for batch in service.search_stream(customer_id=customer_id, query=gaql):
        rows.extend(batch.results)
    return rows


def enum_name(client, value, enum_type: str) -> str:
    """proto-plus enums expose .name; a raw int needs the wrapper lookup."""
    if hasattr(value, "name"):
        return value.name
    try:
        wrapper = getattr(client.enums, enum_type)
        inner = getattr(wrapper, enum_type[:-4] if enum_type.endswith("Enum") else enum_type)
        return inner.Name(int(value))
    except Exception:  # noqa: BLE001 — an unreadable enum must not pass the guard
        return str(value)


def account_tz_offset(client, customer_id: str) -> str:
    """The account's UTC offset as '+02:00', read from the API, never guessed."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    rows = query(client, customer_id, "SELECT customer.time_zone FROM customer LIMIT 1")
    if not rows:
        raise RuntimeError("could not read customer.time_zone")
    tz = ZoneInfo(rows[0].customer.time_zone)
    offset = _dt.now(tz).utcoffset()
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def describe_action(client, customer_id: str, action_id: str) -> dict:
    rows = query(client, customer_id, f"""
        SELECT conversion_action.id, conversion_action.name, conversion_action.type,
               conversion_action.status, conversion_action.category
        FROM conversion_action WHERE conversion_action.id = {int(action_id)}""")
    if not rows:
        raise RuntimeError(f"conversion action {action_id} does not exist on {customer_id}")
    a = rows[0].conversion_action
    return {"id": str(a.id), "name": a.name,
            "type": enum_name(client, a.type_, "ConversionActionTypeEnum"),
            "status": enum_name(client, a.status, "ConversionActionStatusEnum"),
            "category": enum_name(client, a.category, "ConversionActionCategoryEnum")}


def check_mode_matches_action(mode: str, action: dict) -> None:
    allowed = MODE_ACTION_TYPES[mode]
    if action["type"] not in allowed:
        raise SystemExit(
            f"ERROR: --mode {mode} writes to a {'/'.join(sorted(allowed))} action, but "
            f"'{action['name']}' is {action['type']}.\n"
            f"Uploading anyway is accepted by the API and records nothing. "
            f"Create the right action type, or switch mode:\n"
            f"  phone leads from a Google forwarding number -> --mode calls (UPLOAD_CALLS)\n"
            f"  a stored gclid                               -> --mode clicks (UPLOAD_CLICKS)\n"
            f"  no click id, match on email/phone            -> --mode enhanced (UPLOAD_CLICKS)")
    if action["status"] != "ENABLED":
        raise SystemExit(f"ERROR: conversion action '{action['name']}' is {action['status']}, "
                         f"not ENABLED. Uploads to it are discarded.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summary(rows: list[dict], skipped: list[dict], duplicates: list[dict],
            action: dict, mode: str, live: bool) -> str:
    total = sum(r["value"] for r in rows)
    currency = rows[0]["currency"] if rows else ""
    out = [
        f"# Offline conversions — {'LIVE UPLOAD' if live else 'DRY RUN'}",
        "",
        f"Action   : {action['name']} ({action['type']}, {action['status']})",
        f"Mode     : {mode}",
        f"Uploading: {len(rows)} conversions, {total:,.2f} {currency}",
        "",
    ]
    if rows:
        out += ["| Line | Label | Value | Conversion time | Matched on |",
                "|---|---|---|---|---|"]
        for r in rows[:50]:
            if mode == "calls":
                matched = f"{r['caller_id']} at {r['call_start_time'][11:19]}"
            elif mode == "clicks":
                matched = f"{r['click_id_field']} {r['click_id'][:16]}…"
            else:
                parts = [p for p in (("email" if r.get("hashed_email") else None),
                                     ("phone" if r.get("hashed_phone") else None)) if p]
                matched = "hashed " + "+".join(parts)
            out.append(f"| {r['line']} | {r['label']} | {r['value']:,.2f} | "
                       f"{r['conversion_time'][:19]} | {matched} |")
        if len(rows) > 50:
            out.append(f"| … | _{len(rows) - 50} more rows not shown_ | | | |")
        out.append("")

    if duplicates:
        dropped_value = sum(r["value"] for r in duplicates)
        out += [f"## Duplicates dropped: {len(duplicates)} ({dropped_value:,.2f} {currency})",
                "Same identifier and same timestamp as a row already counted. "
                "Uploading them would have inflated the account's revenue.", ""]
        out += [f"- line {r['line']}: {r['label']}" for r in duplicates[:20]] + [""]

    if skipped:
        reasons = Counter(r["reason"] for r in skipped)
        out += [f"## Skipped: {len(skipped)} rows", ""]
        out += [f"- {count}x {reason}" for reason, count in reasons.most_common()] + [""]

    if not live:
        out.append("Nothing was sent. Re-run with --live to upload.")
    return "\n".join(out)


def rejected_rows(rows: list[dict], failure, failure_type) -> list[dict]:
    """Turn a partial_failure_error into 'which of my rows, and why'.

    Without this the API answers 'some rows failed' and the operator has no way to
    know which revenue never landed.
    """
    out = []
    if not failure or not getattr(failure, "message", ""):
        return out
    for detail in getattr(failure, "details", []):
        try:
            parsed = failure_type.deserialize(detail.value)
        except Exception:  # noqa: BLE001
            continue
        for error in parsed.errors:
            index = None
            for element in getattr(error.location, "field_path_elements", []):
                if element.field_name == "conversions":
                    index = element.index
            row = rows[index] if index is not None and index < len(rows) else {}
            out.append({"line": row.get("line"), "label": row.get("label"),
                        "value": row.get("value"), "message": error.message})
    return out


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def upload(client, customer_id: str, action_resource: str, rows: list[dict],
           mode: str) -> tuple[int, list[dict]]:
    service = client.get_service("ConversionUploadService")
    ops = []
    for r in rows:
        if mode == "calls":
            op = client.get_type("CallConversion")
            op.caller_id = r["caller_id"]
            op.call_start_date_time = r["call_start_time"]
        else:
            op = client.get_type("ClickConversion")
            if mode == "clicks":
                setattr(op, r["click_id_field"], r["click_id"])
            else:
                identifiers = []
                if r.get("hashed_email"):
                    ident = client.get_type("UserIdentifier")
                    ident.hashed_email = r["hashed_email"]
                    identifiers.append(ident)
                if r.get("hashed_phone"):
                    ident = client.get_type("UserIdentifier")
                    ident.hashed_phone_number = r["hashed_phone"]
                    identifiers.append(ident)
                op.user_identifiers.extend(identifiers)
        op.conversion_action = action_resource
        op.conversion_date_time = r["conversion_time"]
        op.conversion_value = r["value"]
        op.currency_code = r["currency"]
        ops.append(op)

    caller = (service.upload_call_conversions if mode == "calls"
              else service.upload_click_conversions)
    kwargs = {"customer_id": customer_id, "conversions": ops, "partial_failure": True}
    response = caller(**kwargs)
    rejects = rejected_rows(rows, response.partial_failure_error,
                            client.get_type("GoogleAdsFailure"))
    accepted = len(ops) - len({r["line"] for r in rejects if r.get("line")})
    return accepted, rejects


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gads_offline.py",
        description="Upload offline conversions (real revenue) to Google Ads from a CSV.")
    p.add_argument("--customer-id", required=True, help="Account id, e.g. 123-456-7890")
    p.add_argument("--conversion-action", required=True, help="Conversion action id (digits)")
    p.add_argument("--mode", required=True, choices=sorted(MODE_ACTION_TYPES),
                   help="clicks (gclid) | calls (forwarding number) | enhanced (hashed email/phone)")
    p.add_argument("--csv", required=True, help="Path to the CSV")
    p.add_argument("--currency", default="EUR", help="Fallback currency code (default: EUR)")
    p.add_argument("--default-country", default="",
                   help="Dial code for national phone numbers, e.g. 33")
    p.add_argument("--keep-duplicates", action="store_true",
                   help="Do not drop repeated conversions (you almost never want this)")
    p.add_argument("--live", action="store_true", help="Actually upload. Off by default.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    csv_path = Path(args.csv).expanduser()
    if not csv_path.is_file():
        sys.exit(f"ERROR: no such file: {csv_path}")

    customer_id = re.sub(r"\D", "", args.customer_id)
    if len(customer_id) != 10:
        sys.exit(f"ERROR: --customer-id expects 10 digits, got {args.customer_id!r}")

    raw_rows = read_csv(csv_path)
    missing = REQUIRED_COLUMNS[args.mode] - set(raw_rows[0]) if raw_rows else set()
    if missing:
        sys.exit(f"ERROR: --mode {args.mode} needs these CSV columns: {', '.join(sorted(missing))}")

    client = get_client()
    action = describe_action(client, customer_id, args.conversion_action)
    check_mode_matches_action(args.mode, action)

    tz_offset = account_tz_offset(client, customer_id)
    rows, skipped = build_rows(raw_rows, args.mode, tz_offset,
                               args.currency, args.default_country)
    duplicates: list[dict] = []
    if not args.keep_duplicates:
        rows, duplicates = dedupe(rows, args.mode)

    print(summary(rows, skipped, duplicates, action, args.mode, args.live))

    if not args.live:
        return 0
    if not rows:
        sys.exit("ERROR: nothing uploadable in this file.")

    action_resource = client.get_service("ConversionActionService").conversion_action_path(
        customer_id, action["id"])
    accepted, rejects = upload(client, customer_id, action_resource, rows, args.mode)

    print(f"\nUploaded {accepted}/{len(rows)} conversions to '{action['name']}'.")
    if rejects:
        print(f"\n{len(rejects)} row(s) rejected by Google:")
        for r in rejects:
            print(f"  line {r['line']} ({r['label']}, {r['value']}): {r['message']}")
        print("\nThat revenue did NOT land. Fix these rows and re-run — re-uploading an "
              "accepted row is deduplicated by Google, so a re-run is safe.")
    return 1 if rejects and accepted == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
