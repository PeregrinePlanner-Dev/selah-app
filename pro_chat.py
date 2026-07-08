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
    generate_prep_doc, generate_translation_comparison, RECAP_SECTION_KEYS,
)
from pro_auth import login_required, get_user_supabase, get_service_client

pro_chat_bp = Blueprint("pro_chat", __name__, url_prefix="/pro")

# Conservative placeholder caps until real tiers/pricing are live on Stripe --
# every current signup lands on tier_slug='free' via the auto-provisioning
# trigger, so this exists purely to protect against runaway API cost during
# this pre-launch phase, not as a finished pricing decision. Override via env
# var for the default tier; adjust the dict directly as real tiers launch.
#
# Raised 2026-07-08: the original 30/month figure counted every chat turn
# (not "conversations" despite the column name), which meant a single
# multi-message sitting could exhaust an entire month's allowance -- and it
# was stricter than the free anonymous tool's 1,200 turns/day/IP. Since
# every signup today lands on "free", that was the tier actually gating real
# users. New figures keep the same cost-containment intent (still capped,
# still not equal to the free tool's effectively-unlimited ceiling) but no
# longer make creating a Pro account a worse deal than staying anonymous.
TIER_CONVERSATION_CAPS = {
    "free": int(os.environ.get("FREE_TIER_MONTHLY_CAP", "300")),
    "beta": 500,
    "individual": 500,
    "church": 1500,
    "seminary": 1500,
    "berea": 1500,
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
    "You've reached this month's session recap limit for your current plan. "
    "It resets at the start of next month."
)

# Translation Compare is a lighter call than Prep Doc (no history, ~800
# output tokens) but still its own Sonnet call per click, and unlike Prep
# Doc it can plausibly get clicked many times per session (one per source in
# the panel) rather than once at the end -- tracked under its own
# module_slug so browsing translations doesn't eat into someone's ordinary
# conversation allowance, same reasoning as PREP_DOC_MONTHLY_CAP above, just
# a higher flat number to match its lighter, more casual use pattern.
TRANSLATION_COMPARE_MONTHLY_CAP = int(os.environ.get("TRANSLATION_COMPARE_MONTHLY_CAP", "60"))

TRANSLATION_COMPARE_CAP_MESSAGE = (
    "You've reached this month's translation comparison limit for your "
    "current plan. It resets at the start of next month."
)

# Sanity ceiling on the reference string itself -- this is a person clicking
# a citation already surfaced in their own Source Material panel, not a free
# text field, but the route still accepts whatever the client sends. A real
# reference ("1 Corinthians 15:3-8 (NIV)") is always well under this; this
# just caps prompt-injection-by-length and stray token cost from a
# malformed request, the same spirit as validating any other client input.
MAX_REFERENCE_LENGTH = 120


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


# Matches engine.parse_response()'s own SOURCE-tag regex exactly -- reused
# here (rather than imported) since this operates on a LIST of historical
# messages, not a single raw response.
_SOURCE_TAG_RE = re.compile(r'\[SOURCE:(scripture|theologian)\|(.*?)\|(.*?)\]', re.DOTALL)


def _backfill_sources_from_history(convo: dict) -> list:
    """Reconstructs a session's sources list from its raw message history.

    Sessions started before source persistence existed (2026-07-08,
    8b375bc) -- or any session whose only citations were introduced before
    that fix went live -- have an empty or missing convo['sources'] even
    though every [SOURCE:type|label|content] tag the model ever emitted is
    still sitting verbatim in convo['messages']: strip_tags() only removes
    these when a message is sent back to the model as history, never in the
    persisted record itself. Found 2026-07-08 after a Teaching Outline
    generation correctly pulled real citations straight from the transcript
    text while the Source Material panel stayed empty on the same resumed
    session -- confirming the raw tags were there all along, just never
    backfilled into the dedicated sources list. Dedupes the same way a live
    turn does (first-seen wins, oldest turn first)."""
    sources = []
    seen = set()
    for m in convo.get("messages", []):
        if m.get("role") != "assistant":
            continue
        for match in _SOURCE_TAG_RE.finditer(m.get("content", "")):
            label = match.group(2).strip()
            norm = _normalize_source_label(label)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            sources.append({
                "type": match.group(1),
                "label": label,
                "content": match.group(3).strip(),
            })
    return sources


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

    # The model only ever sees the last MAX_HISTORY messages -- fine for a
    # single sitting, but on a resumed session (or any conversation longer
    # than 8 turns) that means real continuity beyond what's in this window
    # is visible in the UI's Session Anchor panel but invisible to the model
    # itself. Added 2026-07-08: pass the persisted anchor (this turn's prior
    # value, before it gets refreshed below) as its own system block so the
    # model has a running sense of the conversation's arc, not just its own
    # short-term window. Kept as a SEPARATE block from the node-based system
    # prompt (which stays cache_control'd) rather than concatenated into it,
    # so the large static prompt keeps its prompt-cache hit rate -- only this
    # small, per-turn-changing block is sent fresh each time.
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if convo.get("anchor"):
        system_blocks.append({
            "type": "text",
            "text": (
                "Context from earlier in this ongoing conversation (the person "
                "may be picking up a past session -- use this for continuity, "
                "but respond naturally to their latest message below):\n"
                f"{convo['anchor']}"
            ),
        })

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system_blocks,
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
    """Generate a structured recap document (text summary, source material,
    citations, discussion questions) from a saved session -- the first real
    'mode' beyond ordinary chat. Added 2026-07-08 as the first build toward
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

    # Section picker: the client sends whichever of RECAP_SECTION_KEYS the
    # user checked (Text Summary / Source Material / Citations / Discussion
    # Questions). None/omitted means "not specified" -> generate_prep_doc()
    # falls back to all four for backward compatibility. An empty or
    # entirely-invalid list is a real user error (every checkbox unchecked),
    # not silently treated as "give me everything" -- that would be a
    # surprising bait-and-switch after they deliberately unchecked things.
    requested_sections = data.get("sections")
    sections = None
    if requested_sections is not None:
        if not isinstance(requested_sections, list) or not requested_sections:
            return jsonify({"error": "Select at least one section to include."}), 400
        sections = [s for s in requested_sections if s in RECAP_SECTION_KEYS]
        if not sections:
            return jsonify({"error": "Select at least one section to include."}), 400

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
        return jsonify({"error": "Nothing to create a session recap from yet -- have a conversation first."}), 400

    doc_text = generate_prep_doc(
        convo.get("node") or "Grace",
        convo["messages"],
        convo.get("sources", []),
        sections=sections,
    )
    return jsonify({"doc": doc_text})


@pro_chat_bp.route("/compare_translation", methods=["POST"])
@login_required
def compare_translation():
    """Renders one Scripture reference across NIV/ESV/KJV/NASB plus a short
    note on whether the wording differences carry real theological weight --
    "Noticing When the Words Themselves Matter." Added 2026-07-08 as the
    second real 'mode' beyond ordinary chat, alongside Prep Doc.

    Deliberately stateless: doesn't touch planning_sessions at all, doesn't
    require a session_id, and isn't tied to whatever node is active -- a
    reference is a reference regardless of which conversation it surfaced
    in. Only the usage-cap gate needs the caller's organization_id."""
    data = request.json or {}
    reference = (data.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "reference required"}), 400
    if len(reference) > MAX_REFERENCE_LENGTH:
        return jsonify({"error": "reference too long"}), 400

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
        organization_id, tier_slug, module_slug="translation_compare", cap=TRANSLATION_COMPARE_MONTHLY_CAP
    ):
        return jsonify({"error": TRANSLATION_COMPARE_CAP_MESSAGE}), 429

    result = generate_translation_comparison(reference)
    return jsonify({
        "reference": reference,
        "translations": result["translations"],
        "note": result["note"],
    })


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


@pro_chat_bp.route("/coverage", methods=["GET"])
@login_required
def coverage():
    """Aggregates which theology nodes and theologians have actually
    surfaced across ALL of the user's sessions -- the first real piece of
    ministry.html's 'For Solo, Ongoing Study' pitch (a running picture of
    what's been covered), which was previously just "browse your session
    list and infer it yourself." Added 2026-07-08.

    Read-only: sources are backfilled in-memory here (same
    _backfill_sources_from_history() used by get_session()) for any session
    that hasn't been individually resumed yet, so a session's theologians
    still count toward coverage even if nobody has clicked into it since the
    persistence/backfill fixes landed. Deliberately does NOT write the
    backfill back to the DB from here -- that only happens on an actual
    resume (get_session), keeping this endpoint a simple aggregate read
    rather than an N-way write across every session on each call."""
    sb = get_user_supabase()
    resp = sb.table("planning_sessions").select("session_data").execute()

    nodes_covered = set()
    theologians = set()
    for row in resp.data:
        convo = row.get("session_data") or {}
        node = convo.get("node")
        if node:
            nodes_covered.add(node)

        sources = convo.get("sources") or []
        if not sources and convo.get("messages"):
            sources = _backfill_sources_from_history(convo)
        for s in sources:
            if s.get("type") == "theologian":
                label = (s.get("label") or "").strip()
                if label:
                    theologians.add(label)

    return jsonify({
        "nodes_covered": sorted(nodes_covered),
        "nodes_total": len(NODES),
        "theologians": sorted(theologians),
    })


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

    # Backfill sources for sessions that predate persistence (or whose only
    # citations were introduced before that fix went live) -- see
    # _backfill_sources_from_history() docstring. Only runs when the list is
    # genuinely empty, and persists the result so this is a one-time cost
    # per session, not repeated on every resume.
    if not convo.get("sources") and convo.get("messages"):
        backfilled = _backfill_sources_from_history(convo)
        if backfilled:
            convo["sources"] = backfilled
            sb.table("planning_sessions").update({
                "session_data": convo,
            }).eq("id", session_id).execute()

    return jsonify({
        "id": row["id"],
        "messages": convo.get("messages", []),
        "node": convo.get("node"),
        "anchor": convo.get("anchor"),
        "sources": convo.get("sources", []),
        "turn": row["turn_count"],
    })


@pro_chat_bp.route("/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_session(session_id):
    """Permanently deletes one of the logged-in user's own sessions -- a real
    hard delete, no soft-delete/archive tier (decided 2026-07-08: hiding a row
    from the list without deleting it would need its own retrieval view to be
    meaningful, which isn't worth the surface area; and once deleted, the
    session is genuinely gone, not recoverable, matching the confirmation
    copy the frontend shows before calling this).

    Uses get_user_supabase() (the caller's own RLS-scoped token), same as
    get_session() above -- RLS's 'users can delete their own sessions' policy
    (user_id = auth.uid(), added 2026-07-08 alongside this route; it didn't
    exist before, only INSERT/UPDATE/SELECT policies did) scopes this to the
    caller's own rows automatically. Deleting someone else's session_id
    simply matches zero rows, never another user's data -- this route never
    needs an explicit ownership check of its own because the database enforces
    it. Returns ok even if the row was already gone (deleting something
    that's already deleted isn't an error from the client's point of view)."""
    sb = get_user_supabase()
    sb.table("planning_sessions").delete().eq("id", session_id).execute()
    return jsonify({"ok": True})


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
