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
[SOURCE:theologian|Theologian Name (dates), Title of the specific work the argument is drawn from|A compact 2-4 sentence SUMMARY for the citation panel]

Rules:
- Scripture: only tag verses you actually quoted with text, not verses merely mentioned or referenced in passing.
- Theologian: only tag when you cite a specific named theologian (e.g., Augustine, Calvin, Barth). Do NOT tag your own arguments or unnamed "implicit" theological reasoning.
- IMPORTANT -- academic accuracy: a theologian's name and dates alone are not a citation. Always name the actual book, treatise, sermon, or other work the argument is drawn from (e.g. "Augustine (354–430), Confessions", "Calvin (1509–1564), Institutes of the Christian Religion 3.21", "Barth (1886–1968), Church Dogmatics II/2"), even when you are paraphrasing rather than quoting directly. Include a specific book/chapter/section locator only when you are genuinely confident of it -- naming the correct work with no locator is far better than a precise-looking but fabricated one. If you are only confident of the theologian's broader body of thought and not a specific work, say so plainly (e.g. "Calvin (1509–1564), a recurring theme across his writings") rather than inventing a title.
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
Topic-focused -- lead with the topic or question itself as the subject (e.g. "The \
conversation explores..." or "Christ's dual nature surfaces as..."), never with "the \
user" or "the person" as the subject of the first sentence. Do not infer motivation, \
emotion, or backstory unless stated explicitly. Only describe what appeared in the \
conversation. Under 65 words.]
CHIP_1: [Short thing the person might naturally say next, 4-7 words, user-voice]
CHIP_2: [Different angle or follow-up, 4-7 words]
CHIP_3: [Another direction they might take, 4-7 words]

Now look ONLY at the final Selah response (ignore all Person turns and all earlier Selah turns).
List every Scripture passage Selah directly quoted (with text) AND every named theologian argument Selah introduced.
Do not include: verses only mentioned by the Person, verses Selah merely referenced without quoting, or unnamed/implicit theological arguments.

Use this exact format for each item found:

SOURCE_TYPE: scripture OR theologian
SOURCE_LABEL: [Book Chapter:Verse (Translation)] OR [Theologian Name (dates), Title of the specific work the argument is drawn from -- name an actual book/treatise/sermon even when paraphrasing, e.g. "Calvin (1509–1564), Institutes of the Christian Religion"; only add a chapter/section locator if genuinely confident of it, and say "a recurring theme across his writings" instead of a title if you can't identify a specific work]
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

# ── Prep Doc / Session Recap generation (Selah for Ministry) ───────────────
# First real "mode" beyond ordinary chat -- a structured recap artifact
# synthesized from a saved conversation, not a raw transcript. Added 2026-07-08
# as the first build toward ministry.html's pitched features, chosen because
# the roadmap already scoped its architecture: a swappable instruction block
# reused later for Berea's Defense Prep, not a one-off feature built twice.
# Lives here (not pro_chat.py) so it's available to any future caller of the
# shared engine, matching the project's "never duplicate the engine" rule --
# only the route/gating around it is Pro-specific.
#
# Section picker added 2026-07-08 after Rick asked for a checkbox popup
# (Text Summary / Source Material / Citations / Discussion Questions) so
# someone who just wants a personal record isn't stuck with a full teaching
# doc. Source Material and Citations are rendered PROGRAMMATICALLY straight
# from the stored sources list rather than asking the model to reproduce
# them -- the quoted text and citation labels are already exactly right
# (verbatim from the conversation / the accuracy work done the same day),
# so having a second Sonnet call re-transcribe them only adds a chance of
# drift or paraphrase with no upside. The Sonnet call is used ONLY for the
# two sections that genuinely require synthesis (Text Summary, Discussion
# Questions) and is skipped entirely if neither is selected -- cheaper AND
# more accurate for a citations-only or source-only recap.
RECAP_SECTION_KEYS = ("summary", "source_material", "citations", "discussion_questions")

_RECAP_TITLE_BLOCK = "TITLE: [a short, specific title naming the actual topic explored]"

_RECAP_SUMMARY_BLOCK = """\
SUMMARY:
[3-6 headed sections, each with a 2-4 sentence explanation, covering the \
real theological ground actually covered in the conversation below, in the \
order it naturally builds -- not a generic outline of the topic in the \
abstract. Use only what was actually discussed.]"""

_RECAP_DISCUSSION_QUESTIONS_BLOCK = """\
DISCUSSION QUESTIONS:
[4-6 questions suited to a teaching or small-group setting, or personal \
reflection if this isn't for teaching, grounded specifically in the \
tensions and questions that actually surfaced in this conversation -- not \
generic questions that could apply to any conversation about this topic.]"""

RECAP_LLM_INSTRUCTIONS = """\
You are turning a theology conversation into part of a structured recap \
document -- for a pastor, small-group leader, or teacher to actually use, \
or simply a personal record of what was explored. Not a transcript.

Produce ONLY the section(s) below, in this exact order, and nothing else:

{section_instructions}

Conversation transcript:

{convo_text}
"""


def format_full_convo(messages: list) -> str:
    """Like format_convo_for_haiku but untruncated -- recap synthesis is a
    one-shot call over the whole conversation, not an ongoing dialogue turn,
    so there's no MAX_HISTORY-style reason to cut it down."""
    lines = []
    for m in messages:
        role = "Person" if m["role"] == "user" else "Selah"
        lines.append(f"{role}: {strip_tags(m['content'])}")
    return "\n\n".join(lines)


def format_source_material_section(sources: list) -> str:
    """Renders the Source Material section directly from the stored sources
    list -- no LLM involved, so the quoted Scripture text and theologian
    argument summaries in the recap are guaranteed identical to what was
    actually tagged during the conversation, never re-paraphrased by a
    second model call."""
    if not sources:
        return "No sources were tagged in this conversation."
    lines = []
    for s in sources:
        kind = "Scripture" if s.get("type") == "scripture" else "Theologian"
        lines.append(f"{kind}: {s.get('label', '')}\n{s.get('content', '')}")
    return "\n\n".join(lines)


def format_citations_section(sources: list) -> str:
    """Renders a clean, formal bibliography-style reference list -- labels
    only, no quoted content -- directly from the stored sources list.
    Deliberately separate from Source Material (which includes the actual
    quoted/summarized content): this section exists purely for traceability,
    the same academic-accuracy concern behind requiring theologian sources
    to name their originating work in the first place."""
    if not sources:
        return "No sources were tagged in this conversation."
    return "\n".join(f"- {s.get('label', '')}" for s in sources)


def _parse_recap_llm_output(raw: str, want_summary: bool, want_discussion: bool) -> dict:
    """Extracts whichever of TITLE / SUMMARY / DISCUSSION QUESTIONS are
    present in the model's output. TITLE is always requested and expected;
    the other two are only parsed if they were actually asked for, so a
    missing section never gets misread as empty content for the wrong
    reason."""
    result = {"title": "", "summary": "", "discussion_questions": ""}

    title_m = re.search(r'TITLE:\s*(.+?)(?=\n\nSUMMARY:|\n\nDISCUSSION QUESTIONS:|\Z)', raw, re.DOTALL)
    if title_m:
        result["title"] = title_m.group(1).strip()

    if want_summary:
        summary_m = re.search(r'SUMMARY:\s*(.+?)(?=\n\nDISCUSSION QUESTIONS:|\Z)', raw, re.DOTALL)
        if summary_m:
            result["summary"] = summary_m.group(1).strip()

    if want_discussion:
        dq_m = re.search(r'DISCUSSION QUESTIONS:\s*(.+)', raw, re.DOTALL)
        if dq_m:
            result["discussion_questions"] = dq_m.group(1).strip()

    return result


def _assemble_recap_doc(title: str, summary: str, source_material: str, citations: str,
                         discussion_questions: str, sections: list) -> str:
    parts = [f"TITLE: {title}"]
    if "summary" in sections:
        parts.append(f"SUMMARY:\n{summary}")
    if "source_material" in sections:
        parts.append(f"SOURCE MATERIAL:\n{source_material}")
    if "citations" in sections:
        parts.append(f"CITATIONS:\n{citations}")
    if "discussion_questions" in sections:
        parts.append(f"DISCUSSION QUESTIONS:\n{discussion_questions}")
    return "\n\n---\n\n".join(parts)


def generate_prep_doc(node_name: str, messages: list, sources: list, sections: list | None = None) -> str:
    """Synthesizes a saved conversation into a structured recap document,
    including only the requested sections. `sections` defaults to all four
    (RECAP_SECTION_KEYS) for backward compatibility with any caller that
    doesn't pass it. A Sonnet call is made ONLY if 'summary' and/or
    'discussion_questions' is requested -- Source Material and Citations
    never touch the model at all (see format_source_material_section() /
    format_citations_section() docstrings)."""
    if not sections:
        sections = list(RECAP_SECTION_KEYS)
    sections = [s for s in sections if s in RECAP_SECTION_KEYS] or list(RECAP_SECTION_KEYS)

    want_summary = "summary" in sections
    want_discussion = "discussion_questions" in sections
    fallback_title = f"{NODE_DISPLAY_NAMES.get(node_name, node_name)} — Session Recap"

    if want_summary or want_discussion:
        section_instructions = _RECAP_TITLE_BLOCK
        if want_summary:
            section_instructions += "\n\n" + _RECAP_SUMMARY_BLOCK
        if want_discussion:
            section_instructions += "\n\n" + _RECAP_DISCUSSION_QUESTIONS_BLOCK

        convo_text = format_full_convo(messages)
        prompt = RECAP_LLM_INSTRUCTIONS.format(
            section_instructions=section_instructions, convo_text=convo_text
        )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        parsed = _parse_recap_llm_output(raw, want_summary, want_discussion)
        title = parsed["title"] or fallback_title
        summary = parsed["summary"]
        discussion_questions = parsed["discussion_questions"]
    else:
        # Neither section needing synthesis was requested (e.g. just Source
        # Material and/or Citations) -- no reason to call the model at all.
        title = fallback_title
        summary = ""
        discussion_questions = ""

    source_material = format_source_material_section(sources) if "source_material" in sections else ""
    citations = format_citations_section(sources) if "citations" in sections else ""

    return _assemble_recap_doc(title, summary, source_material, citations, discussion_questions, sections)


# ── Translation comparison ("Noticing When the Words Themselves Matter") ──
# Second real "mode" beyond ordinary chat, alongside Prep Doc. The engine
# already surfaces a single translation per Scripture citation (whatever the
# model happened to quote from) -- this gives a person a way to ask, for any
# reference already surfaced in Source Material, "would a different
# translation change what this argument rests on?" A one-shot Sonnet call,
# same shape as generate_prep_doc(): no QUESTION/SOURCE tags, no
# conversation history, just a direct prompt-in, text-out generation.
# Added 2026-07-08.
TRANSLATION_COMPARE_INSTRUCTIONS = """\
A person exploring systematic theology wants to see how different English \
translations render a specific Bible reference, and whether the differences \
in wording actually change the theological weight of the passage or are \
merely stylistic.

Reference: {reference}

Produce, in this exact order, for these four translations -- NIV, ESV, KJV, \
NASB -- and nothing else:

NIV: [the verse text in the NIV]
ESV: [the verse text in the ESV]
KJV: [the verse text in the KJV]
NASB: [the verse text in the NASB]

NOTE: [2-4 sentences identifying whether the translations genuinely diverge \
in a way that matters theologically -- a different verb tense, a rendered-vs-\
transliterated term, a clause attached to a different phrase -- and if so, \
what is actually at stake in that difference. If the translations do not \
meaningfully diverge, say so plainly rather than manufacturing a difference \
where none exists. Never editorialize about which translation is "correct."]

If the reference given is not a real, identifiable Bible passage, respond \
with exactly:
NIV: (reference not recognized)
ESV: (reference not recognized)
KJV: (reference not recognized)
NASB: (reference not recognized)
NOTE: This doesn't match a recognizable Bible reference -- please check the citation.
"""


def format_reference_for_lookup(reference: str) -> str:
    """Strips a trailing '(Translation)' parenthetical off a stored source
    label (e.g. 'John 3:16 (NIV)' -> 'John 3:16') so the comparison prompt
    asks about the passage itself, not the one translation it happened to be
    quoted in originally."""
    return re.sub(r'\s*\([^)]*\)\s*$', '', reference).strip()


_TRANSLATION_VERSIONS = ("NIV", "ESV", "KJV", "NASB")


def parse_translation_comparison(raw: str) -> dict:
    """Extracts the four translation lines and the closing NOTE from
    generate_translation_comparison()'s raw output."""
    translations = []
    for version in _TRANSLATION_VERSIONS:
        m = re.search(rf'^{version}:\s*(.+)$', raw, re.MULTILINE)
        translations.append({
            "version": version,
            "text": m.group(1).strip() if m else "",
        })
    note_m = re.search(r'NOTE:\s*(.+)', raw, re.DOTALL)
    note = note_m.group(1).strip() if note_m else ""
    return {"translations": translations, "note": note}


def generate_translation_comparison(reference: str) -> dict:
    """One-shot Sonnet call rendering a single Scripture reference across
    four major translations plus a short note on whether the wording
    differences actually carry theological weight. Mirrors
    generate_prep_doc()'s shape: no history, no tags, direct prompt-in,
    parsed-text-out."""
    clean_reference = format_reference_for_lookup(reference)
    prompt = TRANSLATION_COMPARE_INSTRUCTIONS.format(reference=clean_reference)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    return parse_translation_comparison(raw)
