"""Selah for Ministry -- Pro chat route, backed by planning_sessions instead
of the free tool's in-memory dict.

Reuses the shared engine (engine.py) for node routing, system-prompt assembly,
and response parsing -- conversational behavior is identical to the free
tool, only where the conversation state lives is different. Added 2026-07-07
as the first real use of the planning_sessions table built the same day.

Deliberately minimal for this pass: no cap-check gate yet (that's the next
task), no export/citation formatting yet -- just proving persistent,
account-scoped sessions work end to end.
"""

import re

from flask import Blueprint, request, jsonify, session

from engine import (
    NODES, MAX_HISTORY, route_to_node, build_system_prompt,
    parse_response, format_convo_for_haiku, ANCHOR_CHIPS_QUERY,
    strip_tags, client as anthropic_client,
)
from pro_auth import login_required, get_user_supabase

pro_chat_bp = Blueprint("pro_chat", __name__, url_prefix="/pro")


def _empty_convo() -> dict:
    return {"messages": [], "node": None, "anchor": "", "turn": 0}


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

    profile_resp = sb.table("profiles").select("organization_id").limit(1).execute()
    if not profile_resp.data:
        return jsonify({"error": "no profile found for this account"}), 400
    organization_id = profile_resp.data[0]["organization_id"]

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


@pro_chat_bp.route("/sessions", methods=["GET"])
@login_required
def list_sessions():
    """Bare list of the logged-in user's own sessions -- proves persistence
    works end to end. Not the real session-history UI, which is a separate,
    later build."""
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
