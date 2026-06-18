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

# ── Keyword routing ───────────────────────────────────────────────────────────
# More specific / culturally-loaded terms must come BEFORE generic ones
# (e.g. "racism" before "bible"; "vocation" before "work")
ROUTING = [
    # High-priority specifics — must come before broad terms that would swallow them
    (["racism", "racial", "race and", "race in", "sexuality", "lgbtq", "gender identity",
      "social justice", "immigration", "climate"],            "Social Ethics"),
    (["baptism", "communion", "eucharist", "sacrament", "lord's supper", "lords supper"], "Sacraments and Ordinances"),
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
    (["baptism", "communion", "eucharist", "sacrament"],      "Sacraments and Ordinances"),
    (["resurrection", "empty tomb", "raised"],                "Resurrection"),
    (["end times", "millennium", "rapture", "eschato"],       "Eschatology"),
    (["prayer", "pray", "intercession"],                      "Prayer"),
    (["suffer", "providence", "why does god allow", "grief"], "Suffering and Providence"),
    (["evil", "theodicy", "problem of pain"],                 "Problem of Evil"),
    (["creation", "evolution", "genesis", "ex nihilo"],       "Creation"),
    (["law and gospel", "legalism", "antinomian"],            "Law and Gospel"),
    (["angel", "demon", "satan", "spiritual warfare"],        "Angels Demons and Spiritual Warfare"),
    (["covenant", "dispensation", "federal"],                 "Covenant Theology"),
    # Broad social/work terms last — catch-all after specifics
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
Do not mention these tags in your conversational reply — they are stripped before display.

[QUESTION: your closing question or reflection invitation]

If you quoted a Scripture passage this turn:
[SOURCE:scripture|Reference (translation)|Full quoted text]

If you introduced a specific theologian's argument this turn:
[SOURCE:theologian|Theologian Name (dates)|The core argument in 2–4 sentences]

If neither Scripture nor a theologian was introduced, omit the SOURCE tag entirely.
Only tag content you actually introduced in THIS response, not previous turns.
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

SOURCE_EXTRACT_PROMPT = """Did the response above introduce a Scripture passage (actually quoted, not just referenced) OR a theologian's argument (with real substance, not just a name drop)?

If yes to Scripture, respond with exactly:
TYPE: scripture
LABEL: [Book Chapter:Verse (Translation)]
CONTENT: [the quoted text]

If yes to Theologian, respond with exactly:
TYPE: theologian
LABEL: [Name (dates)]
CONTENT: [the argument in 2–3 sentences]

If neither was substantively introduced, respond with exactly:
NONE

No other text."""

# Combined anchor + dynamic chips — one Haiku call per turn
ANCHOR_CHIPS_QUERY = """\
Here is a theology conversation:

{convo_text}

Return your response in this EXACT format — nothing before or after:

ANCHOR: [2–3 sentences about what is being explored and what has actually surfaced. \
Topic-focused — do not say "the user." Do not infer motivation, emotion, or backstory \
unless the person stated it explicitly. Only describe what appeared in the conversation. \
Under 65 words.]
CHIP_1: [Short thing the person might naturally say next, 4–7 words, user-voice]
CHIP_2: [Different angle or follow-up, 4–7 words]
CHIP_3: [Another direction they might take, 4–7 words]"""


def format_convo_for_haiku(messages: list, max_chars: int = 3000) -> str:
    """Flatten conversation history to a readable text block, tags stripped."""
    lines = []
    for m in messages:
        role = "Person" if m["role"] == "user" else "TES"
        content = re.sub(r'\[QUESTION:.*?\]', '', m["content"], flags=re.DOTALL)
        content = re.sub(r'\[SOURCE:.*?\]',   '', content,      flags=re.DOTALL).strip()
        lines.append(f"{role}: {content}")
    return "\n\n".join(lines)[:max_chars]

def parse_response(raw: str) -> dict:
    """Strip structured tags from Claude's reply and extract them separately."""
    question = ""
    source   = None

    # Extract [QUESTION: ...]
    q_match = re.search(r'\[QUESTION:\s*(.*?)\]', raw, re.DOTALL)
    if q_match:
        question = q_match.group(1).strip()
        raw = raw[:q_match.start()].strip()

    # Extract [SOURCE:type|label|content]
    s_match = re.search(r'\[SOURCE:(scripture|theologian)\|(.*?)\|(.*?)\]', raw, re.DOTALL)
    if s_match:
        source = {
            "type":    s_match.group(1),
            "label":   s_match.group(2).strip(),
            "content": s_match.group(3).strip(),
        }
        raw = raw[:s_match.start()].strip() + raw[s_match.end():].strip()

    return {"reply": raw.strip(), "question": question, "source": source}

# ── In-memory conversations ───────────────────────────────────────────────────
conversations: dict = {}
# structure: {messages: [], node: str, anchor: str, turn: int}

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

    # Determine active node
    if force_node and force_node in NODES:
        convo["node"] = force_node
    elif convo["node"] is None:
        convo["node"] = route_to_node(message)

    active_node = convo["node"]
    system      = build_system_prompt(active_node)

    convo["messages"].append({"role": "user", "content": message})

    # Main response
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=convo["messages"],
    )
    raw_text = response.content[0].text
    parsed   = parse_response(raw_text)

    convo["messages"].append({"role": "assistant", "content": raw_text})
    convo["turn"] += 1

    # Fallback anchor — shows only if the Haiku call below fails
    if not convo["anchor"]:
        convo["anchor"] = f"Exploring {active_node}."

    # Combined anchor + dynamic chips — one Haiku call
    chips = []
    try:
        convo_text = format_convo_for_haiku(convo["messages"])
        haiku_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
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
    except Exception as e:
        print(f"[ANCHOR/CHIPS ERROR] {e}")

    # Extract source material — pass last TES response as data, not as message history
    source = parsed["source"]  # fallback to tag-parsed if present
    if source is None:
        try:
            last_assistant = next(
                (m["content"] for m in reversed(convo["messages"]) if m["role"] == "assistant"),
                ""
            )
            extract_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": (
                        "Here is a theology response:\n\n"
                        + last_assistant[:2000]
                        + "\n\n"
                        + SOURCE_EXTRACT_PROMPT
                    )
                }],
            )
            extract_text = extract_resp.content[0].text.strip()
            if not extract_text.startswith("NONE"):
                lines = extract_text.splitlines()
                src = {}
                for line in lines:
                    if line.startswith("TYPE:"):
                        src["type"] = line.split(":", 1)[1].strip()
                    elif line.startswith("LABEL:"):
                        src["label"] = line.split(":", 1)[1].strip()
                    elif line.startswith("CONTENT:"):
                        src["content"] = line.split(":", 1)[1].strip()
                if all(k in src for k in ("type", "label", "content")):
                    source = src
        except Exception:
            pass

    return jsonify({
        "reply":    parsed["reply"],
        "question": parsed["question"],
        "source":   source,
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
    convo   = conversations.get(sid, {})
    msgs    = convo.get("messages", [])
    anchor  = convo.get("anchor", "")
    node    = convo.get("node", "")

    lines = [f"TES Session Export\nNode: {node}\n\n=== Session Anchor ===\n{anchor}\n\n=== Conversation ===\n"]
    for m in msgs:
        role = "You" if m["role"] == "user" else "TES"
        # Strip the structured tags from exported text
        text = re.sub(r'\[QUESTION:.*?\]', '', m["content"], flags=re.DOTALL)
        text = re.sub(r'\[SOURCE:.*?\]',   '', text,         flags=re.DOTALL).strip()
        lines.append(f"{role}:\n{text}\n")

    return jsonify({"text": "\n".join(lines)})

@app.route("/upload_session", methods=["POST"])
def upload_session():
    """Seed a new session from a previously downloaded recap file."""
    data       = request.json
    session_id = data.get("session_id")
    content    = data.get("content", "")

    # Extract node from file if present
    node = "Grace"
    node_match = re.search(r"Node:\s*(.+)", content)
    if node_match:
        found = node_match.group(1).strip()
        if found in NODES:
            node = found

    # Extract previous anchor if present
    prev_anchor = ""
    anchor_match = re.search(r"=== Session Anchor ===(.*?)=== Conversation ===", content, re.DOTALL)
    if anchor_match:
        prev_anchor = anchor_match.group(1).strip()

    # Generate returning-session greeting via Claude
    returning_prompt = (
        "You are TES — the Theology Exploration System. "
        "A user is returning from a previous session. "
        "Here is their previous session recap:\n\n"
        + content[:2000]
        + "\n\nWrite a brief, warm returning-session opening (2–3 sentences): "
        "recap the key tension or question from last time, then ask one reflection prompt — "
        "has anything shifted since they last explored this? "
        "Do not use headers or bullet points. Plain conversational text only."
    )

    greeting_resp = cl