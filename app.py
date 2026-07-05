import os
import re
import uuid
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app    = Flask(__name__)
client = Anthropic()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
NODES_DIR  = BASE_DIR / "nodes"
PROMPT_DIR = BASE_DIR / "prompt"

# ── Load master prompt & nodes at startup ─────────────────────────────────────
MASTER_PROMPT = (PROMPT_DIR / "TES_Master_Prompt_v1.md").read_text(encoding="utf-8")

NODES = {}
for f in sorted(NODES_DIR.glob("*.md")):
    NODES[f.stem] = f.read_text(encoding="utf-8")
NODE_NAMES = sorted(NODES.keys())

# ── Conversation history cap ───────────────────────────────────────────────────
# Full history kept in memory for export; only last MAX_HISTORY messages sent to
# Sonnet to prevent unbounded input-token growth.
MAX_HISTORY = 8

# ── Keyword routing ───────────────────────────────────────────────────────────
ROUTING = [
    (["racism", "racial", "race and", "race in", "sexuality", "lgbtq", "gender identity",
      "social justice", "immigration", "climate"],            "Social Ethics"),
    (["baptis", "communion", "eucharist", "sacrament", "lord's supper", "lords supper"], "Sacraments and Ordinances"),
    (["vocation", "calling", "my job", "my career", "my work", "does god care about my"],
                                                              "Vocation and Work"),
    (["miracle", "healing", "cessation", "supernatural"],    "Miracles"),
    (["regenerat", "born again", "new birth"],               "Regeneration"),
    (["grace"],                                               "Grace"),
    (["sin", "sinful", "fallen", "depravity"],                "Sin"),
    (["faith", "belief", "believe", "trust"],                 "Faith"),
    (["justif", "righteous", "imputed"],                      "Justification"),
    (["sanctif", "holiness", "transform", "grow"],            "Sanctification"),
    (["atonement", "propitiation", "redemption"],             "Atonement"),
    (["christolog", "hypostatic", "incarnat"],                "Christology"),
    (["trinity", "triune", "three persons"],                  "Trinity"),
    (["holy spirit", "pneuma", "pentecost", "tongues"],       "Holy Spirit"),
    (["scripture", "bible", "inerrancy", "hermeneutic"],      "Scripture and Revelation"),
    (["omnipotent", "omniscient", "immutable", "attribute"],  "Theology Proper"),
    (["assurance", "know i am saved", "certain"],             "Assurance"),
    (["predestination", "election", "free will", "sovereignty"], "Sovereignty and Free Will"),
    (["imago dei", "image of god", "human nature"],           "Anthropology"),
    (["church", "ecclesi", "congregation"],                   "Ecclesiology"),
    (["resurrection", "empty tomb", "raised"],                "Resurrection"),
    (["end times", "millennium", "rapture", "eschato"],       "Eschatology"),
    (["prayer", "pray", "intercession"],                      "Prayer"),
    (["suffer", "providence", "why does god allow", "grief"], "Suffering and Providence"),
    (["evil", "theodicy", "problem of pain"],                 "Problem of Evil"),
    (["creation", "evolution", "genesis", "ex nihilo"],       "Creation"),
    (["law and gospel", "legalism", "antinomian"],            "Law and Gospel"),
    (["angel", "demon", "satan", "spiritual warfare"],        "Angels Demons and Spiritual Warfare"),
    (["covenant", "dispensation", "federal"],                 "Covenant Theology"),
    (["justice", "poverty", "politics", "race"],              "Social Ethics"),
    (["work", "job", "career", "purpose"],                    "Vocation and Work"),
]

def route_to_node(message: str) -> str:
    msg = message.lower()
    for keywords, node in ROUTING:
        if any(kw in msg for kw in keywords):
            if node in NODES:
                return node
    return "Grace"

# ── Response format instructions appended to system prompt ────────────────────
RESPONSE_FORMAT = """

---

## Technical Output Format (invisible to user, parsed by UI)

After your response, append a structured block using these exact tags.
Do not mention these tags in your conversational reply -- they are stripped before display.

[QUESTION: your closing question or reflection invitation]

For each Scripture passage you directly quoted AND each named theologian's argument you introduced THIS turn, append one tag:
[SOURCE:scripture|Book Chapter:Verse (Translation)|Full quoted text]
[SOURCE:theologian|Theologian Name (dates)|The core argument in 2-4 sentences]

Rules:
- Scripture: only tag verses you actually quoted with text, not verses merely mentioned or referenced in passing.
- Theologian: only tag when you cite a specific named theologian (e.g., Augustine, Calvin, Barth). Do NOT tag your own arguments or unnamed "implicit" theological reasoning.
- You may include multiple SOURCE tags per turn.
- Only tag content first introduced in THIS response — never re-tag from prior turns.
- If you introduced nothing new this turn, omit SOURCE tags entirely.
"""

def build_system_prompt(node_name: str) -> str:
    node_content = NODES.get(node_name, "")
    return (
        MASTER_PROMPT
        + "\n\n---\n\n"
        + f"## Active Node: {node_name}\n\n"
        + "Use the content below as your primary doctrinal and tension reference.\n\n"
        + node_content
        + RESPONSE_FORMAT
    )

# ── Combined anchor + chips + source extraction -- one Haiku call per turn ────
# Source extraction is folded in here, eliminating the separate second Haiku call.
ANCHOR_CHIPS_QUERY = """\
Here is a theology conversation:

{convo_text}

Return your response in this EXACT format -- nothing before or after:

ANCHOR: [2-3 sentences about what is being explored and what has actually surfaced. \
Topic-focused -- do not say "the user." Do not infer motivation, emotion, or backstory \
unless the person stated it explicitly. Only describe what appeared in the conversation. \
Under 65 words.]
CHIP_1: [Short thing the person might naturally say next, 4-7 words, user-voice]
CHIP_2: [Different angle or follow-up, 4-7 words]
CHIP_3: [Another direction they might take, 4-7 words]

Now look ONLY at the final Selah response (ignore all Person turns and all earlier Selah turns).
List every Scripture passage Selah directly quoted (with text) AND every named theologian argument Selah introduced.
Do not include: verses only mentioned by the Person, verses Selah merely referenced without quoting, or unnamed/implicit theological arguments.

Use this exact format for each item found:

SOURCE_TYPE: scripture OR theologian
SOURCE_LABEL: [Book Chapter:Verse (Translation)] OR [Theologian Name (dates)]
SOURCE_CONTENT: [exact quoted text] OR [the argument in 2-3 sentences]
SOURCE_END

Repeat the block for each item. If the final Selah response contains nothing qualifying, output: SOURCE_TYPE: none"""


def format_convo_for_haiku(messages: list, max_chars: int = 3000) -> str:
    """Flatten conversation history to a readable text block, tags stripped."""
    lines = []
    for m in messages:
        role = "Person" if m["role"] == "user" else "Selah"
        content = re.sub(r'\[QUESTION:.*?\]', '', m["content"], flags=re.DOTALL)
        content = re.sub(r'\[SOURCE:.*?\]',   '', content,      flags=re.DOTALL).strip()
        lines.append(f"{role}: {content}")
    text = "\n\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text

def parse_response(raw: str) -> dict:
    """Strip structured tags from Claude's reply and extract them separately.

    IMPORTANT: SOURCE tags must be collected from the ORIGINAL raw text, before any
    truncation. RESPONSE_FORMAT instructs the model to emit [QUESTION: ...] first and
    [SOURCE: ...] tags after it -- truncating on the QUESTION tag's position before
    searching for SOURCE tags silently discards every source Sonnet ever tags. (Found
    2026-07-05: this masked Sonnet's own source tagging entirely; the app was running
    on the Haiku fallback extraction exclusively.)
    """
    question = ""
    sources  = []

    # Collect ALL source tags from the full raw text first, regardless of tag order.
    for m in re.finditer(r'\[SOURCE:(scripture|theologian)\|(.*?)\|(.*?)\]', raw, re.DOTALL):
        sources.append({
            "type":    m.group(1),
            "label":   m.group(2).strip(),
            "content": m.group(3).strip(),
        })

    q_match = re.search(r'\[QUESTION:\s*(.*?)\]', raw, re.DOTALL)
    if q_match:
        question = q_match.group(1).strip()

    # The reply is everything before the first technical tag (QUESTION or SOURCE),
    # whichever comes first -- not just before QUESTION.
    tag_starts = []
    if q_match:
        tag_starts.append(q_match.start())
    first_source_match = re.search(r'\[SOURCE:(scripture|theologian)\|', raw)
    if first_source_match:
        tag_starts.append(first_source_match.start())
    cut = min(tag_starts) if tag_starts else len(raw)
    reply = raw[:cut].strip()

    return {"reply": reply, "question": question, "sources": sources}

# ── In-memory conversations ───────────────────────────────────────────────────
conversations: dict = {}

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", nodes=NODE_NAMES)

@app.route("/chat", methods=["POST"])
def chat():
    data       = request.json
    message    = data.get("message", "").strip()
    session_id = data.get("session_id")
    force_node = data.get("node")

    if not message:
        return jsonify({"error": "empty message"}), 400

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
    system      = build_system_prompt(active_node)

    convo["messages"].append({"role": "user", "content": message})

    # ── Main response (Sonnet) ────────────────────────────────────────────────
    # system prompt is cached -- saves ~80-90% of input token cost from turn 2 onward.
    # Only the last MAX_HISTORY messages are sent to cap growing context costs.
    # Strip technical tags from history so Sonnet doesn't see prior [SOURCE:] tags
    # and interpret them as "sourcing already done" -- which caused it to stop tagging.
    def strip_tags(text: str) -> str:
        text = re.sub(r'\[QUESTION:.*?\]', '', text, flags=re.DOTALL)
        text = re.sub(r'\[SOURCE:.*?\]',   '', text, flags=re.DOTALL)
        return text.strip()

    clean_history = [
        {"role": m["role"], "content": strip_tags(m["content"])}
        for m in convo["messages"][-MAX_HISTORY:]
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1536,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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
