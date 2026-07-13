"""Selah for Ministry — transactional email, added 2026-07-13.

Thin wrapper around the Resend Python SDK. Kept as its own module for the
same reason pro_org.py/pro_billing.py/pro_auth.py are separate -- additive
only, nothing here touches the free tool.

Sending domain: selahexploringtheology.com, verified in Resend under a
dedicated account (separate from the sibling "Peregrine" project's own
Resend account -- Rick's call, 2026-07-13: free-tier accounts are capped at
1 verified domain each, and keeping usage/billing isolated per project is
good practice regardless of that cap).

Same "clean no-op, not a crash" pattern as pro_billing.py's Stripe wiring:
if RESEND_API_KEY isn't set, send_email() logs and returns False instead of
raising, so a missing/misconfigured key never breaks the real action (e.g.
a roster removal still succeeds even if the notice email fails to send --
the access change is real regardless of whether the person was told about
it by email).
"""

import os

import resend

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
resend.api_key = RESEND_API_KEY

# "send" subdomain -- matches the MX/SPF/DKIM records Resend's Cloudflare
# auto-configure added under selahexploringtheology.com (2026-07-13).
FROM_ADDRESS = "Selah for Ministry <notifications@send.selahexploringtheology.com>"


def send_email(to: str, subject: str, html: str) -> bool:
    """Sends one email. Returns True if Resend accepted it, False on any
    failure (missing key, API error, etc.) -- callers should treat email as
    best-effort and never let a failure here block the real state change
    that triggered it."""
    if not RESEND_API_KEY:
        print(f"[EMAIL] RESEND_API_KEY not set -- skipping send to {to!r} (subject: {subject!r})")
        return False
    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send to {to!r} (subject: {subject!r}): {e}")
        return False


def _wrap(body_html: str) -> str:
    """Minimal shared shell -- plain, sentence-case, no marketing tone,
    matching the app's own working-dashboard-not-a-sales-pitch principle."""
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:#111827; line-height:1.5; max-width:480px;">
      {body_html}
      <p style="color:#6b7280; font-size:0.8rem; margin-top:24px;">Selah for Ministry</p>
    </div>
    """


def send_roster_removal_email(to: str, org_name: str) -> bool:
    """Sent when an admin removes someone from a church org's roster
    (pro_org.py remove_from_roster()) -- they're moved to their own
    individual trial (14 days / 25 exchanges), same as any fresh signup."""
    subject = f"You've been removed from {org_name} on Selah for Ministry"
    html = _wrap(f"""
      <p>You've been removed from <strong>{org_name}</strong>'s roster on Selah for Ministry.</p>
      <p>Your seat there is gone, but your account still works -- you've been moved to your own individual trial (14 days, 25 exchanges) so you're not locked out.</p>
      <p>If this wasn't expected, contact {org_name}'s admin directly.</p>
    """)
    return send_email(to, subject, html)


def send_waitlist_promoted_email(to: str, org_name: str, seat_type: str) -> bool:
    """Sent when someone waitlisted for a seat gets auto-promoted
    (pro_billing.py promote_waitlisted_if_room()) -- can be triggered by
    someone else's action entirely (an admin buying more seats, or another
    member being removed), so this is the only way the promoted person
    would find out without logging in on the chance of checking."""
    kind = "Leadership" if seat_type == "leader" else "Membership"
    subject = f"A seat opened up at {org_name}"
    html = _wrap(f"""
      <p>Good news -- a {kind} seat opened up at <strong>{org_name}</strong> on Selah for Ministry, and you've been moved off the waitlist.</p>
      <p>Sign in any time to start using it.</p>
    """)
    return send_email(to, subject, html)
