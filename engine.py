"""Selah's shared conversation engine -- node content, routing, system-prompt
assembly, and response parsing.

Extracted from app.py 2026-07-07 so the free tool and the new Pro chat route
(pro_chat.py) both import the SAME engine rather than each having their own
copy. This is the concrete implementation of the architecture decision made
the same day: the underlying node content and conversation engine stay
shared, never duplicated, only the UI/routes differ per tier.

Nothing in this file's logic has changed from what previously lived inline
in app.py -- this is a pure relocation, not a rewrite.
"""

import os
import re
from pathlib import Path
from anthropic import Anthropic

client = Anthropic()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
NODES_DIR  = BASE_DIR / "nodes"
PROMPT_DIR = BASE_DIR / "prompt"

# ── Load master prompt & nodes at startup ─────────────────────────────────────
MASTER_PROMPT = (PROMPT_DIR / "TES_Master_Prompt_v1.md").read_text(encoding="utf-8")

NODES = {}
NODE_DISPLAY_NAMES = {}
for f in sorted(NODES_DIR.glob("*.md")):
    text = f.read_text(encoding="utf-8")
    NODES[f.stem] = text
    # Derive a clean display name from the file's own H1 (e.g. "# Node: Heresy, False
    # Teachers & the Great Apostasy" -> "Heresy, False Teachers & the Great Apostasy"),
    # stripping any parenthetical subtitle. Falls back to the raw stem if no H1 is found.
    # This keeps the node badge showing a proper title instead of the internal file-stem
    # key (e.g. "Heresy False Teachers and the Great Apostasy" with no punctuation) --
    # raised 2026-07-06 after Rick flagged the badge as confusing without context.
    first_line = text.splitlines()[0] if text else ""
    m = re.match(r'^#\s*(?:Node:\s*)?(.+?)(?:\s*\([^)]*\))?\s*$', first_line)
    NODE_DISPLAY_NAMES[f.stem] = m.group(1).strip() if m else f.stem
NODE_NAMES = sorted(NODES.keys())

# ── Conversation history cap ───────────────────────────────────────────────────
# Full history kept for export; only last MAX_HISTORY messages sent to
# Sonnet to prevent unbounded input-token growth.
MAX_HISTORY = 8

# ── Keyword routing ───────────────────────────────────────────────────────────
ROUTING = [
    (["heresy", "heretic", "heretical", "false teacher", "false teachers", "false prophet",
      "false prophets", "false doctrine", "apostasy", "apostate", "falling away",
      "great apostasy", "arianism", "testing the spirits", "test the spirits",
      "discernment ministry", "contend for the faith"],       "Heresy False Teachers and the Great Apostasy"),
    (["evangelis", "great commission", "share my faith", "share the gospel",
      "share christ", "spread the gospel", "witness", "win souls", "winning souls",
      "only way to god", "only way to heaven", "is jesus the only way",
      "exclusivity of christ", "tell others about jesus"],    "Evangelism and Mission"),
    (["racism", "racial", "race and", "race in", "sexuality", "lgbtq", "gender identity",
      "social justice", "immigration", "climate"],            "Social Ethics"),
    (["baptis", "baptiz", "communion", "eucharist", "sacrament", "lord's supper", "lords supper"], "Sacraments and Ordinances"),
    (["vocation", "calling", "called to ministry", "my job", "my career", "my work", "does god care about my"],
                                                              "Vocation and Work"),
    (["miracle", "healing", "cessation", "supernatural"],    "Miracles"),
    (["regenerat", "born again", "new birth"],               "Regeneration"),
    (["grace"],                                               "Grace"),
    (["sin", "sinful", "fallen", "depravity"],                "Sin"),
    (["faith", "belief", "believe", "trust"],                 "Faith"),
    (["justif", "righteous", "imputed"],                      "Justification"),
    (["sanctif", "holiness", "transform", "grow"],            "Sanctification"),
    (["atonement", "propitiation", "redemption", "why did jesus have to die", "jesus have to die", "the cross"], "Atonement"),
    (["christolog", "hypostatic", "incarnat", "was jesus god", "is jesus god", "jesus really god", "who is jesus", "fully god and fully human"], "Christology"),
    (["trinity", "triune", "three persons"],                  "Trinity"),
    (["holy spirit", "pneuma", "pentecost", "tongues"],       "Holy Spirit"),
    (["scripture", "bible", "inerrancy", "hermeneutic", "canon"], "Scripture and Revelation"),
    (["omnipotent", "omniscient", "immutable", "attribute", "wrath of god", "is god angry"], "Theology Proper"),
    (["assurance", "know i am saved", "am i saved", "am i really saved", "really saved", "don't feel saved", "unforgivable sin", "certain"], "Assurance"),
    (["predestination", "predetermined", "election", "free will", "sovereignty"], "Sovereignty and Free Will"),
    (["imago dei", "image of god", "human nature", "does my body matter"], "Anthropology"),
    (["church", "ecclesi", "congregation", "denomination"],   "Ecclesiology"),
    (["resurrection", "empty tomb", "raised"],                "Resurrection"),
    (["end times", "millennium", "rapture", "eschato", "heaven", "afterlife", "is hell real", "hell forever"], "Eschatology"),
    (["prayer", "pray", "intercession"],                      "Prayer"),
    (["suffer", "providence", "why does god allow", "grief"], "Suffering and Providence"),
    (["evil", "theodicy", "problem of pain"],                 "Problem of Evil"),
    (["creation", "evolution", "genesis", "ex nihilo", "age of the earth"], "Creation"),
    (["law and gospel", "legalis", "antinomian"],             "Law and Gospel"),
    (["angel", "demon", "satan", "spiritual warfare", "occult", "witchcraft", "wicca", "tarot", "astrology", "possessed", "exorcis", "ouija"], "Angels Demons and Spiritual Warfare"),
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
[SOURCE:theologian|Theologian Name (dates)|A compact 2-4 sentence SUMMARY for the citation panel]

Rules:
- Scripture: only tag verses you actually quoted with text, not verses merely mentioned or referenced in passing.
- Theologian: only tag when you cite a specific named theologian (e.g., Augustine, Calvin, Barth). Do NOT tag your own arguments or unnamed "implicit" theological reasoning.
- IMPORTANT: the SOURCE tag's 2-4 sentence summary is a compact citation for the side panel ONLY. It is separate from, and must never replace or shorten, your actual conversational engagement with the theologian's argument. Your conversational reply itself should still give the full 2-4 paragraphs of real substance described in the Theologian Engine section above -- write that first, in full, then add this short tag afterward as a pointer back to it.
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

def strip_tags(text: str) -> str:
    """Remove [QUESTION:...] and [SOURCE:...] tags from a message before it's
    sent back to the model as history -- prevents Sonnet from seeing its own
    prior [SOURCE:] tags and concluding sourcing is already done."""
    text = re.sub(r'\[QUESTION:.*?\]', '', text, flags=re.DOTALL)
    text = re.sub(r'\[SOURCE:.*?\]',   '', text, flags=re.DOTALL)
    return text.strip()
