import os
import re
from collections import defaultdict
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

from pro_auth import pro_bp
from pro_chat import pro_chat_bp
from pro_billing import pro_billing_bp
from engine import (
    NODES, NODE_DISPLAY_NAMES, NODE_NAMES, MAX_HISTORY,
    route_to_node, build_system_blocks, parse_response,
    format_convo_for_haiku, ANCHOR_CHIPS_QUERY, strip_tags, client,
)

load_dotenv()

app = Flask(__name__)

# Selah for Ministry (Pro) auth -- additive only, registered as a separate
# blueprint under /pro/*. The free tool's existing routes below are
# untouched by this. Added 2026-07-07.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-insecure-key-change-me")
app.register_blueprint(pro_bp)
app.register_blueprint(pro_chat_bp)
app.register_blueprint(pro_billing_bp)

# Node content, routing, system-prompt assembly (build_system_blocks), and
# response parsing (parse_response) all live in engine.py now (extracted
# 2026-07-07) -- the free tool and the Pro chat route (pro_chat.py) share
# one engine instead of each keeping its own copy. Nothing about their
# behavior changed in this refactor, only where the code lives.

# ── Anonymous abuse/cost cap ───────────────────────────────────────────────────
# Free tier has no accounts, so this is IP-based -- not tied to identity,
# nothing persisted beyond the current minute/day, purely a guard against
# runaway/bot API cost. Two dimensions, not one:
#
#   1. Burst/rate limit (per IP per minute) -- this is the REAL abuse signal.
#      Genuine automated abuse is characterized by request RATE, not just total
#      volume. A shared connection with many real people on it at once -- e.g. a
#      youth group meeting where a leader's login/network serves a whole room --
#      is paced by human typing speed and won't trip this even though many
#      distinct people are using it. (Raised 2026-07-05, Session 20: the original
#      flat daily-only cap didn't account for exactly this "one identifier, many
#      real humans" shape, which the congregation/youth-group access model
#      creates by design.)
#   2. Daily cap (per IP) -- a looser backstop against slow, sustained abuse that
#      deliberately stays under the burst threshold but runs for hours.
#
# Once real Pro/church accounts exist, authenticated institutional traffic should
# be metered against that organization's own subscription cap (see
# usage_records/conversations_cap in Selah_Pro_Infrastructure_Plan.md) instead of
# this anonymous IP limiter -- this block is a free/anonymous-tier safety net
# only, not meant to apply once someone is on a paid, authenticated plan.
# Decided 2026-07-05 (Session 20 roadmap item) -- see DEVELOPMENT_ROADMAP.md.
MINUTE_RATE_CAP = int(os.environ.get("MINUTE_RATE_CAP", "30"))
DAILY_TURN_CAP  = int(os.environ.get("DAILY_TURN_CAP", "1200"))

minute_tracker: dict = defaultdict(dict)  # {ip: {"YYYY-MM-DDTHH:MM": count}}
usage_tracker:  dict = defaultdict(dict)  # {ip: {"YYYY-MM-DD": count}}

RATE_LIMIT_MESSAGE_BURST = (
    "Selah's getting a lot of messages from this connection all at once -- "
    "give it just a moment and try again."
)
RATE_LIMIT_MESSAGE_DAILY = (
    "Selah's seen a lot of company today, so replies from this connection are "
    "paused until tomorrow to keep things running smoothly for everyone. "
    "Thanks for your patience -- come back soon."
)

def get_client_ip() -> str:
    """Real client IP behind Render's proxy, falling back to remote_addr."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"

def check_rate_limit(ip: str) -> str | None:
    """Returns None if this request is allowed (and increments both counters as
    a side effect), or a reason string ('burst' or 'daily') if it should be
    blocked. Call exactly once per billable API call (i.e. once per /chat
    request, once per /upload_session request), not once per underlying
    Anthropic call."""
    now    = datetime.utcnow()
    today  = now.date().isoformat()
    minute = now.strftime("%Y-%m-%dT%H:%M")

    ip_day = usage_tracker[ip]
    for d in list(ip_day.keys()):        # keep only today's entry -- self-cleaning
        if d != today:
            del ip_day[d]

    ip_minute = minute_tracker[ip]
    for m in list(ip_minute.keys()):     # keep only the current minute's entry
        if m != minute:
            del ip_minute[m]

    if ip_minute.get(minute, 0) >= MINUTE_RATE_CAP:
        return "burst"
    if ip_day.get(today, 0) >= DAILY_TURN_CAP:
        return "daily"

    ip_minute[minute] = ip_minute.get(minute, 0) + 1
    ip_day[today]     = ip_day.get(today, 0) + 1
    return None

# ── In-memory conversations ───────────────────────────────────────────────────
conversations: dict = {}

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Once ministry.selahexploringtheology.com DNS points at this same Render
    # service, requests arriving on that host serve the landing page instead
    # of the main app -- no separate hosting/service needed. Until DNS is
    # live, preview at /ministry on the existing domain.
    if request.host.startswith("ministry."):
        return render_template("ministry.html")
    return render_template("index.html", nodes=NODE_NAMES, node_display_names=NODE_DISPLAY_NAMES)

@app.route("/ministry")
def ministry():
    return render_template("ministry.html")

@app.route("/invite")
def invite():
    # Shareable invitation page for the free tool -- built 2026-07-08 at Rick's
    # request after his pastor friend (Clark Cothern) asked for something he
    # could send to his congregation and pastoral network. Additive only, no
    # existing route touched.
    return render_template("invite.html")

@app.route("/legal")
def legal():
    return render_template("legal.html")

@app.route("/chat", methods=["POST"])
def chat():
    data       = request.json
    message    = data.get("message", "").strip()
    session_id = data.get("session_id")
    force_node = data.get("node")

    if not message:
        return jsonify({"error": "empty message"}), 400

    limit_hit = check_rate_limit(get_client_ip())
    if limit_hit:
        msg = RATE_LIMIT_MESSAGE_BURST if limit_hit == "burst" else RATE_LIMIT_MESSAGE_DAILY
        return jsonify({
            "reply":    msg,
            "question": "",
            "sources":  [],
            "node":     "",
            "anchor":   "",
            "chips":    [],
            "turn":     0,
        })

    if session_id not in conversations:
        conversations[session_id] = {
            "messages": [], "node": None, "anchor": "", "turn": 0
        }

    convo = conversations[session_id]

    if force_node and force_node in NODES:
        convo["node"] = force_node
    elif convo["node"] is None:
        convo["node"] = route_to_node(message)

    active_node = convo["node"]

    convo["messages"].append({"role": "user", "content": message})

    # ── Main response (Sonnet) ────────────────────────────────────────────────
    # System prompt is split into independently-cached blocks (see
    # engine.build_system_blocks) -- the MASTER_PROMPT and RESPONSE_FORMAT
    # layers are identical across every node/user app-wide, so they stay warm
    # from ANY request; only the smaller node-specific layer needs re-caching
    # when that node's traffic goes quiet. 1-hour ephemeral TTL (not the
    # 5-minute default) added 2026-07-09 so normal reading/reflection pauses
    # between turns don't force a cache rewrite.
    # Only the last MAX_HISTORY messages are sent to cap growing context costs.
    # Strip technical tags from history so Sonnet doesn't see prior [SOURCE:] tags
    # and interpret them as "sourcing already done" -- which caused it to stop tagging.
    clean_history = [
        {"role": m["role"], "content": strip_tags(m["content"])}
        for m in convo["messages"][-MAX_HISTORY:]
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=build_system_blocks(active_node),
        messages=clean_history,
    )
    raw_text = response.content[0].text
    parsed   = parse_response(raw_text)

    convo["messages"].append({"role": "assistant", "content": raw_text})
    convo["turn"] += 1

    if not convo["anchor"]:
        convo["anchor"] = f"Exploring {active_node}."

    # ── Combined anchor + chips + source -- one Haiku call ────────────────────
    chips   = []
    sources = parsed["sources"]
    try:
        convo_text = format_convo_for_haiku(convo["messages"])
        haiku_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{
                "role": "user",
                "content": ANCHOR_CHIPS_QUERY.format(convo_text=convo_text)
            }],
        )
        haiku_text = haiku_resp.content[0].text.strip()

        anchor_match = re.search(r'ANCHOR:\s*(.+?)(?=\nCHIP_|\Z)', haiku_text, re.DOTALL)
        chip_matches = re.findall(r'CHIP_\d+:\s*(.+)', haiku_text)

        if anchor_match:
            convo["anchor"] = anchor_match.group(1).strip()
        chips = [c.strip() for c in chip_matches if c.strip()]
        convo["chips"] = chips

        # Parse all SOURCE blocks from Haiku if Sonnet tags produced nothing
        if not sources:
            blocks = re.split(r'SOURCE_END', haiku_text)
            for block in blocks:
                type_m    = re.search(r'SOURCE_TYPE:\s*(\S+)',                              block)
                label_m   = re.search(r'SOURCE_LABEL:\s*(.+)',                              block)
                content_m = re.search(r'SOURCE_CONTENT:\s*(.+?)(?=\nSOURCE_|\Z)', block, re.DOTALL)
                t_val = type_m.group(1).strip().lower()   if type_m    else ""
                l_val = label_m.group(1).strip().lower()  if label_m   else ""
                c_val = content_m.group(1).strip().lower() if content_m else ""
                if (type_m and t_val not in ("none", "")
                        and label_m and l_val not in ("none", "none identified", "")
                        and content_m and c_val not in ("none", "none identified", "")):
                    sources.append({
                        "type":    type_m.group(1).strip(),
                        "label":   label_m.group(1).strip(),
                        "content": content_m.group(1).strip(),
                    })

    except Exception as e:
        print(f"[ANCHOR/CHIPS/SOURCE ERROR] {e}")

    return jsonify({
        "reply":    parsed["reply"],
        "question": parsed["question"],
        "sources":  sources,
        "node":     active_node,
        "anchor":   convo["anchor"],
        "chips":    chips,
        "turn":     convo["turn"],
    })

@app.route("/reset", methods=["POST"])
def reset():
    sid = request.json.get("session_id")
    if sid in conversations:
        del conversations[sid]
    return jsonify({"ok": True})

@app.route("/export", methods=["POST"])
def export():
    """Return a plain-text session transcript for saving."""
    data    = request.json
    sid     = data.get("session_id")
    sources = data.get("sources", [])
    convo   = conversations.get(sid, {})
    msgs    = convo.get("messages", [])
    anchor  = convo.get("anchor", "")
    node    = convo.get("node", "")

    lines = [f"Selah Session Export\nNode: {node}\n\n=== Session Anchor ===\n{anchor}\n\n=== Conversation ===\n"]
    for m in msgs:
        role = "You" if m["role"] == "user" else "Selah"
        text = re.sub(r'\[QUESTION:.*?\]', '', m["content"], flags=re.DOTALL)
        text = re.sub(r'\[SOURCE:.*?\]',   '', text,         flags=re.DOTALL).strip()
        lines.append(f"{role}:\n{text}\n")

    if sources:
        lines.append("\n=== Sources Cited ===\n")
        for s in sources:
            kind  = "Scripture" if s.get("type") == "scripture" else "Theologian"
            label = s.get("label", "")
            content = s.get("content", "")
            lines.append(f"{kind} — {label}\n{content}\n")

    return jsonify({"text": "\n".join(lines)})

@app.route("/upload_session", methods=["POST"])
def upload_session():
    """Seed a new session from a previously downloaded recap file."""
    data       = request.json
    session_id = data.get("session_id")
    content    = data.get("content", "")

    limit_hit = check_rate_limit(get_client_ip())
    if limit_hit:
        msg = RATE_LIMIT_MESSAGE_BURST if limit_hit == "burst" else RATE_LIMIT_MESSAGE_DAILY
        return jsonify({
            "greeting": msg,
            "node":     "",
            "anchor":   "",
        })

    node = "Grace"
    node_match = re.search(r"Node:\s*(.+)", content)
    if node_match:
        found = node_match.group(1).strip()
        if found in NODES:
            node = found

    prev_anchor = ""
    anchor_match = re.search(r"=== Session Anchor ===(.*?)=== Conversation ===", content, re.DOTALL)
    if anchor_match:
        prev_anchor = anchor_match.group(1).strip()

    # Parse full conversation into message pairs
    all_messages = []
    convo_match = re.search(r"=== Conversation ===(.*?)(?:=== Sources Cited ===|\Z)", content, re.DOTALL)
    if convo_match:
        convo_text = convo_match.group(1).strip()
        turns = re.split(r'\n(?=You:\n|Selah:\n)', convo_text)
        for turn in turns:
            turn = turn.strip()
            if turn.startswith("You:\n"):
                all_messages.append({"role": "user", "content": turn[5:].strip()})
            elif turn.startswith("Selah:\n"):
                all_messages.append({"role": "assistant", "content": turn[7:].strip()})

    # Build full transcript text for Haiku to summarize
    full_transcript = "\n\n".join(
        f"{'Person' if m['role']=='user' else 'Selah'}: {m['content']}"
        for m in all_messages
    )

    # Have Haiku generate a context brief capturing personal details and key tensions
    context_prompt = (
        "Read this theology conversation carefully and write a compact CONTEXT BRIEF "
        "that a returning conversation partner would need to serve this person well.\n\n"
        "Include:\n"
        "- Key personal details shared (life situation, age, relationships, history, wounds named)\n"
        "- The specific struggles, fears, or unresolved tensions they voiced\n"
        "- The theological themes explored and how they connected to the person's life\n"
        "- The exact question or tension where the conversation ended\n"
        "- Anything they said that carries particular emotional or spiritual weight\n\n"
        "Write in plain prose, 150-200 words. This is for internal context only — not shown to the user.\n\n"
        f"CONVERSATION:\n{full_transcript[:6000]}"
    )

    context_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": context_prompt}],
    )
    context_brief = context_resp.content[0].text.strip()

    # Last 6 exchanges (12 messages) for conversational thread
    recent_messages = all_messages[-12:]

    # Build greeting using context brief + last exchanges
    last_exchanges = "\n\n".join(
        f"{'Person' if m['role']=='user' else 'Selah'}: {m['content']}"
        for m in all_messages[-4:]
    )
    returning_prompt = (
        f"Context brief from prior session:\n{context_brief}\n\n"
        f"Last exchanges:\n{last_exchanges}\n\n"
        "Write a warm returning-session opening of 2-3 sentences only. "
        "Reference the specific tension or question they left unresolved, then ask one focused reflection prompt. "
        "Do NOT mention how much time has passed — you don't know. "
        "No headers. No bullet points. No numbered lists. Plain prose only."
    )

    greeting_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": returning_prompt}],
    )
    greeting = greeting_resp.content[0].text.strip()

    # Seed: hidden context brief, then last 6 exchanges, then greeting
    seed_messages = (
        [{"role": "user",      "content": f"[SESSION CONTEXT — not shown to user:\n{context_brief}]"},
         {"role": "assistant", "content": "Understood. I have the full context from the prior session."}]
        + recent_messages
        + [{"role": "assistant", "content": greeting}]
    )

    conversations[session_id] = {
        "messages": seed_messages,
        "node":   node,
        "anchor": prev_anchor,
        "turn":   0,
    }

    return jsonify({"greeting": greeting, "node": node, "anchor": prev_anchor})

if __name__ == "__main__":
    print(f"Selah running --> http://localhost:5000")
    print(f"Nodes loaded: {len(NODES)}")
    app.run(debug=True, port=5000)
