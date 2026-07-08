"""Selah for Ministry -- Pro chat route, backed by planning_sessions instead
of the free tool's in-memory dict.

Reuses the shared engine (engine.py) for node routing, system-prompt assembly,
and response parsing -- conversational behavior is identical to the free
tool, only where the conversation state lives is different. Added 2026-07-07
as the first real use of the planning_sessions table built the same day.

Cap-check gate added the same day: usage_records is checked BEFORE the
Anthropic call, using the service-role client (never the user's own RLS-scoped
token -- a user's own client must never be able to read-then-reset its own
usage counter). No export/citation formatting yet -- that's a separate,
later build.
"""

import os
import re
from datetime import date

from flask import Blueprint, request, jsonify, session, render_template

from engine import (
    NODES, NODE_NAMES, NODE_DISPLAY_NAMES, MAX_HISTORY, route_to_node,
    build_system_prompt, parse_response, format_convo_for_haiku,
    ANCHOR_CHIPS_QUERY, strip_tags, client as anthropic_client,
    generate_prep_doc,
)
from pro_auth import login_required, get_user_supabase, get_service_client

pro_chat_bp = Blueprint("pro_chat", __name__, url_prefix="/pro")

# Conservative placeholder caps until real tiers/pricing are live on Stripe --
# every current signup lands on tier_slug='free' via the auto-provisioning
# trigger, so this exists purely to protect against runaway API cost during
# this pre-launch phase, not as a finished pricing decision. Override via env
# var for the default tier; adjust the dict directly as real tiers launch.
TIER_CONVERSATION_CAPS = {
    "free": int(os.environ.get("FREE_TIER_MONTHLY_CAP", "30")),
    "beta": 100,
    "individual": 200,
    "church": 200,
    "seminary": 200,
    "berea": 200,
}
DEFAULT_CAP = TIER_CONVERSATION_CAPS["free"]

CAP_HIT_MESSAGE = (
    "You've reached this month's conversation limit for your current plan. "
    "It resets at the start of next month."
)

# Prep Doc is a separate, more expensive action (a full untruncated-history
# Sonnet call) than an ordinary chat turn -- tracked under its own
# module_slug ('prep_mode', matching the module_registry row seeded back in
# Session 21) rather than sharing the chat cap, so generating a few Prep Docs
# doesn't eat into someone's ordinary conversation allowance. Flat cap for
# now, not tier-differentiated, since no real tiers exist yet -- same
# "conservative placeholder" spirit as TIER_CONVERSATION_CAPS above.
PREP_DOC_MONTHLY_CAP = int(os.environ.get("PREP_DOC_MONTHLY_CAP", "10"))

PREP_DOC_CAP_MESSAGE = (
    "You've reached this month's Prep Doc limit for your current plan. "
    "It resets at the start of next month."
)


def _empty_convo() -> dict:
    return {"messages": [], "node": None, "anchor": "", "turn": 0, "sources": []}


def _normalize_source_label(label: str) -> str:
    """Mirrors normalizeLabel() in pro_app.html -- strips parentheticals and
    'referenced/implied/...' suffixes so the same citation introduced across
    different turns (e.g. re-quoted, or tagged with slightly different
    wording) doesn't get stored twice in the persisted sources list."""
    label = re.sub(r'\(.*?\)', '', label)
    label = re.sub(r'\s*(referenced|implied|implicit|unnamed).*$', '', label, flags=re.IGNORECASE)
    return label.lower().strip()


def _billing_month_today() -> str:
    """First of the current month, as an ISO date string -- matches
    usage_records.billing_month's scoping (per org, per module, per
    calendar month)."""
    today = date.today()
    return today.replace(day=1).isoformat()


def _check_and_reserve_usage(
    organization_id: str, tier_slug: str, module_slug: str | None = None, cap: int | None = None
) -> bool:
    """Returns True if this turn is allowed to proceed, False if the org has
    hit its monthly cap. Reserves the turn (increments conversations_used)
    as part of the same call when allowed, using the service-role client --
    this table is never written through the user's own token.

    module_slug scopes the cap to a specific feature (e.g. 'prep_mode')
    instead of the default ordinary-chat bucket (module_slug IS NULL) --
    added 2026-07-08 for Prep Doc, which is a separate, more expensive action
    and shouldn't eat into someone's ordinary conversation allowance. cap
    lets the caller override the tier-based default (Prep Doc uses its own
    flat PREP_DOC_MONTHLY_CAP, not TIER_CONVERSATION_CAPS).

    Known simplification: check-then-write is two round trips, not one
    atomic operation, so a burst of near-simultaneous requests from the same
    org could slightly overshoot the cap under real concurrent load. Fine at
    today's volume; worth revisiting (e.g. a single atomic SQL update) before
    real scale, same spirit as the free tool's existing rate-limit caveats."""
    svc = get_service_client()
    billing_month = _billing_month_today()
    if cap is None:
        cap = TIER_CONVERSATION_CAPS.get(tier_slug, DEFAULT_CAP)

    query = (
        svc.table("usage_records")
        .select("id, conversations_used, conversations_cap, hard_cap")
        .eq("organization_id", organization_id)
        .eq("billing_month", billing_month)
    )
    query = query.is_("module_slug", "null") if module_slug is None else query.eq("module_slug", module_slug)
    existing = query.limit(1).execute()

    if existing.data:
        row = existing.data[0]
        if row["hard_cap"] and row["conversations_used"] >= row["conversations_cap"]:
            return False
        svc.table("usage_records").update({
            "conversations_used": row["conversations_used"] + 1,
        }).eq("id", row["id"]).execute()
        return True
    else:
        svc.table("usage_records").insert({
            "organization_id": organization_id,
            "module_slug": module_slug,
            "billing_month": billing_month,
            "conversations_used": 1,
            "conversations_cap": cap,
            "hard_cap": True,
        }).execute()
        return True


@pro_chat_bp.route("/chat", methods=["POST"])
@login_required
def pro_chat():
    data = request.json or {}
    message = data.get("message", "").strip()
    session_db_id = data.get("session_id")
    force_node = data.get("node")

    if not message:
        return jsonify({"error": "empty message"}), 400

    sb = get_user_supabase()

    profile_resp = sb.table("profiles").select("organization_id, seat_status").limit(1).execute()
    if not profile_resp.data:
        return jsonify({"error": "no profile found for this account"}), 400
    organization_id = profile_resp.data[0]["organization_id"]

    # Comped seats (Rick's own account, the pastor-friend beta tester/marketer)
    # bypass the usage cap entirely, regardless of tier_slug. This is a manual
    # profiles.seat_status flag set directly in Supabase (never through Stripe),
    # checked here before the cap-check gate rather than folded into the
    # subscriptions/tier system -- deliberately kept separate so that whenever
    # Stripe billing sync gets built, it never needs special-case logic for
    # comped accounts: they simply never have Stripe fields populated and
    # this check short-circuits before billing status is ever consulted.
    is_comped = profile_resp.data[0].get("seat_status") == "comped"

    sub_resp = (
        sb.table("subscriptions")
        .select("tier_slug")
        .eq("organization_id", organization_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )
    tier_slug = sub_resp.data[0]["tier_slug"] if sub_resp.data else "free"

    # Cap-check gate -- BEFORE the Anthropic call, never after. Uses the
    # service-role client internally; never checked or written via the
    # user's own token. Comped seats skip this entirely -- no usage-record
    # row is even written for them, since there's nothing to cap.
    if not is_comped and not _check_and_reserve_usage(organization_id, tier_slug):
        return jsonify({
            "reply": CAP_HIT_MESSAGE,
            "question": "",
            "sources": [],
            "node": "",
            "anchor": "",
            "chips": [],
            "turn": 0,
        })

    if session_db_id:
        row_resp = (
            sb.table("planning_sessions")
            .select("session_data")
            .eq("id", session_db_id)
            .limit(1)
            .execute()
        )
        if not row_resp.data:
            return jsonify({"error": "session not found"}), 404
        convo = row_resp.data[0]["session_data"] or _empty_convo()
    else:
        convo = _empty_convo()

    if force_node and force_node in NODES:
        convo["node"] = force_node
    elif convo.get("node") is None:
        convo["node"] = route_to_node(message)

    active_node = convo["node"]
    system = build_system_prompt(active_node)

    convo["messages"].append({"role": "user", "content": message})

    clean_history = [
        {"role": m["role"], "content": strip_tags(m["content"])}
        for m in convo["messages"][-MAX_HISTORY:]
    ]

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=clean_history,
    )
    raw_text = response.content[0].text
    parsed = parse_response(raw_text)

    convo["messages"].append({"role": "assistant", "content": raw_text})
    convo["turn"] = convo.get("turn", 0) + 1

    if not convo.get("anchor"):
        convo["anchor"] = f"Exploring {active_node}."

    chips = []
    sources = parsed["sources"]
    try:
        convo_text = format_convo_for_haiku(convo["messages"])
        haiku_resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{
                "role": "user",
                "content": ANCHOR_CHIPS_QUERY.format(convo_text=convo_text),
            }],
        )
        haiku_text = haiku_resp.content[0].text.strip()

        anchor_match = re.search(r'ANCHOR:\s*(.+?)(?=\nCHIP_|\Z)', haiku_text, re.DOTALL)
        chip_matches = re.findall(r'CHIP_\d+:\s*(.+)', haiku_text)

        if anchor_match:
            convo["anchor"] = anchor_match.group(1).strip()
        chips = [c.strip() for c in chip_matches if c.strip()]

        if not sources:
            blocks = re.split(r'SOURCE_END', haiku_text)
            for block in blocks:
                type_m = re.search(r'SOURCE_TYPE:\s*(\S+)', block)
                label_m = re.search(r'SOURCE_LABEL:\s*(.+)', block)
                content_m = re.search(r'SOURCE_CONTENT:\s*(.+?)(?=\nSOURCE_|\Z)', block, re.DOTALL)
                t_val = type_m.group(1).strip().lower() if type_m else ""
                l_val = label_m.group(1).strip().lower() if label_m else ""
                c_val = content_m.group(1).strip().lower() if content_m else ""
                if (type_m and t_val not in ("none", "")
                        and label_m and l_val not in ("none", "none identified", "")
                        and content_m and c_val not in ("none", "none identified", "")):
                    sources.append({
                        "type": type_m.group(1).strip(),
                        "label": label_m.group(1).strip(),
                        "content": content_m.group(1).strip(),
                    })
    except Exception as e:
        print(f"[PRO ANCHOR/CHIPS/SOURCE ERROR] {e}")

    # Persist this turn's sources into the session so resuming a conversation
    # (get_session) can restore the Source Material panel, not just the
    # transcript and anchor. Deduped by normalized label -- same rule the
    # frontend already uses for its own client-side accumulation.
    convo.setdefault("sources", [])
    existing_norms = {_normalize_source_label(s["label"]) for s in convo["sources"]}
    for s in sources:
        norm = _normalize_source_label(s["label"])
        if norm and norm not in existing_norms:
            convo["sources"].append(s)
            existing_norms.add(norm)

    if session_db_id:
        sb.table("planning_sessions").update({
            "session_data": convo,
            "turn_count": convo["turn"],
            "updated_at": "now()",
        }).eq("id", session_db_id).execute()
    else:
        insert_resp = sb.table("planning_sessions").insert({
            "user_id": session["sb_user_id"],
            "organization_id": organization_id,
            "session_data": convo,
            "turn_count": convo["turn"],
        }).execute()
        session_db_id = insert_resp.data[0]["id"]

    return jsonify({
        "session_id": session_db_id,
        "reply": parsed["reply"],
        "question": parsed["question"],
        "sources": sources,
        "node": active_node,
        "anchor": convo["anchor"],
        "chips": chips,
        "turn": convo["turn"],
    })


@pro_chat_bp.route("/prep_doc", methods=["POST"])
@login_required
def prep_doc():
    """Generate a structured teaching document (outline, citations,
    discussion questions) from a saved session -- the first real 'mode'
    beyond ordinary chat. Added 2026-07-08 as the first build toward
    ministry.html's pitched features; reuses the roadmap's already-scoped
    swappable-instruction-block pattern (see generate_prep_doc() in
    engine.py) rather than inventing a one-off mechanism.

    Known simplification: available to every logged-in Pro account
    regardless of tier -- module_access-based per-org feature gating (the
    schema supports it) isn't wired up anywhere yet, since no tiers actually
    differ from each other today. Revisit once real tiers exist."""
    data = request.json or {}
    session_db_id = data.get("session_id")
    if not session_db_id:
        return jsonify({"error": "session_id required"}), 400

    sb = get_user_supabase()

    profile_resp = sb.table("profiles").select("organization_id, seat_status").limit(1).execute()
    if not profile_resp.data:
        return jsonify({"error": "no profile found for this account"}), 400
    organization_id = profile_resp.data[0]["organization_id"]
    is_comped = profile_resp.data[0].get("seat_status") == "comped"

    sub_resp = (
        sb.table("subscriptions")
        .select("tier_slug")
        .eq("organization_id", organization_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )
    tier_slug = sub_resp.data[0]["tier_slug"] if sub_resp.data else "free"

    if not is_comped and not _check_and_reserve_usage(
        organization_id, tier_slug, module_slug="prep_mode", cap=PREP_DOC_MONTHLY_CAP
    ):
        return jsonify({"error": PREP_DOC_CAP_MESSAGE}), 429

    row_resp = (
        sb.table("planning_sessions")
        .select("session_data")
        .eq("id", session_db_id)
        .limit(1)
        .execute()
    )
    if not row_resp.data:
        return jsonify({"error": "session not found"}), 404
    convo = row_resp.data[0]["session_data"] or _empty_convo()

    if not convo.get("messages"):
        return jsonify({"error": "Nothing to generate a Prep Doc from yet -- have a conversation first."}), 400

    doc_text = generate_prep_doc(
        convo.get("node") or "Grace",
        convo["messages"],
        convo.get("sources", []),
    )
    return jsonify({"doc": doc_text})


@pro_chat_bp.route("/sessions", methods=["GET"])
@login_required
def list_sessions():
    """List of the logged-in user's own sessions, newest first -- backs the
    session-history panel in the real Pro UI (pro_app.html)."""
    sb = get_user_supabase()
    resp = (
        sb.table("planning_sessions")
        .select("id, turn_count, updated_at, session_data")
        .order("updated_at", desc=True)
        .execute()
    )
    sessions = [
        {
            "id": row["id"],
            "turn_count": row["turn_count"],
            "updated_at": row["updated_at"],
            "node": (row.get("session_data") or {}).get("node"),
            "anchor": (row.get("session_data") or {}).get("anchor"),
        }
        for row in resp.data
    ]
    return jsonify({"sessions": sessions})


@pro_chat_bp.route("/sessions/<session_id>", methods=["GET"])
@login_required
def get_session(session_id):
    """Full transcript for one session -- lets the UI resume a past
    conversation instead of just showing it existed. RLS scopes this to the
    caller's own rows automatically; a foreign session_id simply returns
    no rows, not another user's data."""
    sb = get_user_supabase()
    resp = (
        sb.table("planning_sessions")
        .select("id, session_data, turn_count, updated_at")
        .eq("id", session_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return jsonify({"error": "session not found"}), 404
    row = resp.data[0]
    convo = row["session_data"] or _empty_convo()
    return jsonify({
        "id": row["id"],
        "messages": convo.get("messages", []),
        "node": convo.get("node"),
        "anchor": convo.get("anchor"),
        "sources": convo.get("sources", []),
        "turn": row["turn_count"],
    })


@pro_chat_bp.route("/app")
@login_required
def pro_app():
    """The real Selah for Ministry chat UI -- account-aware, session-history
    backed, no donate link, no upload/download file dance. Renders here
    (not in pro_auth.py) since it needs NODES/NODE_DISPLAY_NAMES from the
    shared engine, which pro_chat.py already imports."""
    return render_template(
        "pro_app.html",
        nodes=NODE_NAMES,
        node_display_names=NODE_DISPLAY_NAMES,
        email=session.get("sb_email", ""),
    )
