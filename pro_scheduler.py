"""Selah for Ministry -- scheduled/cron job endpoint, added 2026-07-13
(Task #41: whole-org cancellation cascade + pre-cancellation reminder
emails). This app has no scheduled/cron capability of its own -- nothing
outside a live web request has ever run on a timer here. This blueprint
exists to be hit once a day by an external trigger (a Render Cron Job,
per Rick's 2026-07-13 call: Task #41 is material enough to justify
$1/month over a free third-party pinger and one less platform dependency)
rather than adding a background scheduler thread inside the web process
itself, which a Render web service can't reliably run anyway (the process
isn't guaranteed to stay alive between requests).

Kept as its own module, registered onto the existing Flask app additively,
same pattern as every other pro_*.py blueprint -- nothing here touches the
free tool or any request-triggered Pro route. Both jobs below are
read-then-act queries scoped to a narrow, self-clearing signal
(cancel_at_period_end just flipped true and no reminder sent yet, or
status='canceled' with still-unmigrated roster members) -- re-running this
endpoint on the same day, or twice by accident, is a safe no-op: once a
row's been handled, it no longer matches the query that found it.
"""

import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, request, jsonify, url_for

from pro_auth import get_service_client
from pro_org import transfer_profile_to_individual_trial, _get_org_name
from pro_email import (
    send_roster_removal_email,
    send_cancellation_reminder_email,
    send_cascade_admin_summary_email,
)

pro_scheduler_bp = Blueprint("pro_scheduler", __name__, url_prefix="/pro/internal")

CRON_SECRET = os.environ.get("CRON_SECRET", "")

# "A few days before" -- Rick's own phrasing, 2026-07-13. A single named
# constant rather than buried in the query logic below, so it's easy to
# find and change later without hunting through the function.
REMINDER_LEAD_DAYS = 3


def _parse_ts(value):
    """subscriptions.current_period_end round-trips through Supabase as an
    ISO 8601 string (set via pro_billing.py's _epoch_to_iso(), which
    already produces a '+00:00' offset, not 'Z') -- but tolerate a 'Z'
    suffix too rather than assume, since Postgrest's own serialization has
    changed format before across versions."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _check_cron_secret():
    """Returns an error Flask response if the caller isn't the cron job,
    None if the request is authorized. Fails CLOSED: an unset CRON_SECRET
    blocks every request rather than accidentally leaving this endpoint
    open -- it can migrate real people off a paid roster, this is not a
    read-only route."""
    if not CRON_SECRET:
        return jsonify({"error": "CRON_SECRET not configured -- refusing to run."}), 503
    provided = request.headers.get("Authorization", "")
    if provided != f"Bearer {CRON_SECRET}":
        return jsonify({"error": "unauthorized"}), 401
    return None


def _send_cancellation_reminders(svc) -> int:
    """Job 1 -- warns org admins a few days before a canceled-but-still-
    active church subscription's paid period actually ends. Fires once per
    cancellation window, guarded by cancellation_reminder_sent_at (reset to
    NULL on reactivation -- see pro_billing.py's _sync_church_subscription_row
    / _sync_subscription_row for where that reset happens). Date-range
    filtering is done in Python rather than via .gte()/.lte() query
    chaining -- fewer moving parts to get wrong on a job that touches real
    customer-facing email, easy to reason about from the row data alone."""
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=REMINDER_LEAD_DAYS)

    candidates = (
        svc.table("subscriptions")
        .select("id, organization_id, seat_type, current_period_end")
        .eq("status", "active")
        .eq("cancel_at_period_end", True)
        .is_("cancellation_reminder_sent_at", "null")
        .execute()
    )

    sent = 0
    for row in candidates.data or []:
        if not row.get("seat_type"):
            continue  # individual-tier row -- not part of the Church/Org cascade this job exists for
        if not row.get("current_period_end"):
            continue
        period_end = _parse_ts(row["current_period_end"])
        if not (now <= period_end <= window_end):
            continue
        try:
            admins = (
                svc.table("profiles")
                .select("email")
                .eq("organization_id", row["organization_id"])
                .eq("is_org_admin", True)
                .execute()
            )
            admin_emails = [a["email"] for a in (admins.data or []) if a.get("email")]
            if not admin_emails:
                continue
            org_name = _get_org_name(svc, row["organization_id"])
            period_end_str = period_end.strftime("%B %-d, %Y")
            for email in admin_emails:
                send_cancellation_reminder_email(email, org_name, row["seat_type"], period_end_str)
            svc.table("subscriptions").update({
                "cancellation_reminder_sent_at": now.isoformat()
            }).eq("id", row["id"]).execute()
            sent += 1
        except Exception as e:
            # One org's failure (bad email, transient DB hiccup) should
            # never block the rest of the batch -- log loudly, keep going.
            print(f"[SCHEDULER] reminder failed for subscription {row.get('id')}: {e}")
    return sent


def _run_cancellation_cascade(svc) -> int:
    """Job 2 -- the actual whole-org cascade. Finds church subscriptions
    that have fully ended (status='canceled') and still have roster members
    sitting on that seat_type, migrates every one of them to their own
    individual trial via transfer_profile_to_individual_trial() -- the
    exact same mechanism pro_org.py's remove_from_roster() uses for a
    single person -- emails each person individually, then sends admins
    one summary. Naturally idempotent: once everyone on a (org,seat_type)
    pool is migrated, the roster query below returns empty and this
    becomes a no-op for it on every future run -- no separate "already
    processed" flag needed."""
    rows = (
        svc.table("subscriptions")
        .select("id, organization_id, seat_type")
        .eq("status", "canceled")
        .execute()
    )

    orgs_processed = 0
    for row in rows.data or []:
        seat_type = row.get("seat_type")
        if not seat_type:
            continue  # individual-tier row, or an old pre-migration row with no seat_type recorded
        organization_id = row["organization_id"]
        try:
            roster = (
                svc.table("profiles")
                .select("id, email")
                .eq("organization_id", organization_id)
                .eq("seat_type", seat_type)
                .execute()
            )
            people = roster.data or []
            if not people:
                continue  # already fully migrated -- nothing to do

            # Captured BEFORE migrating -- an admin can themselves hold this
            # same seat_type and get migrated below, which would otherwise
            # make them unfindable by the time the summary email goes out.
            admins = (
                svc.table("profiles")
                .select("email")
                .eq("organization_id", organization_id)
                .eq("is_org_admin", True)
                .execute()
            )
            admin_emails = [a["email"] for a in (admins.data or []) if a.get("email")]
            org_name = _get_org_name(svc, organization_id)

            migrated = 0
            for person in people:
                try:
                    transfer_profile_to_individual_trial(svc, person["id"], person.get("email"))
                    if person.get("email"):
                        upgrade_link = url_for("pro_chat.pro_app", promo="winback", _external=True)
                        send_roster_removal_email(person["email"], org_name, upgrade_link)
                    migrated += 1
                except Exception as e:
                    print(f"[SCHEDULER] cascade transfer failed for profile {person.get('id')}: {e}")

            if migrated:
                orgs_processed += 1
                for email in admin_emails:
                    send_cascade_admin_summary_email(email, org_name, seat_type, migrated)
        except Exception as e:
            print(f"[SCHEDULER] cascade failed for org {organization_id}/{seat_type}: {e}")
    return orgs_processed


@pro_scheduler_bp.route("/run-scheduled-jobs", methods=["POST"])
def run_scheduled_jobs():
    """Single entry point a Render Cron Job hits once a day:
    curl -X POST -H "Authorization: Bearer $CRON_SECRET"
    https://selah-app.onrender.com/pro/internal/run-scheduled-jobs
    Runs both jobs in sequence; one crashing doesn't block the other, and
    each already guards its own per-row failures internally."""
    auth_error = _check_cron_secret()
    if auth_error:
        return auth_error

    svc = get_service_client()

    reminders_sent = 0
    try:
        reminders_sent = _send_cancellation_reminders(svc)
    except Exception as e:
        print(f"[SCHEDULER] reminder job crashed: {e}")

    orgs_cascaded = 0
    try:
        orgs_cascaded = _run_cancellation_cascade(svc)
    except Exception as e:
        print(f"[SCHEDULER] cascade job crashed: {e}")

    return jsonify({
        "ok": True,
        "reminders_sent": reminders_sent,
        "cascades_processed": orgs_cascaded,
    })
