from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import httpx
import os
from ..env import ENV
from ..utils.translate import _infer_subject_from_english, _openai_chat_options
import re

_GUARDRAIL_DEBUG = os.getenv("GUARDRAIL_DEBUG", "").lower() in {"1", "true", "yes"}

FIRST_PERSON_KO_MARKERS = [
    "나는", "난", "내가", "내게", "나를", "나도", "나만", "나와", "나에게", "나한테", "나의",
    "저는", "전", "제가", "제게", "저를", "저도", "저만", "저와", "저에게", "저한테", "저의",
    "우리", "우리가", "우리는", "우릴", "우리의", "우리도", "우리만", "우리와", "우리에게", "우리한테"
]

HE_KO_MARKERS = [
    "그는", "그가", "그를", "그의", "그에게", "그한테",
    "그분은", "그분이", "그분을", "그분의",
]

SHE_KO_MARKERS = [
    "그녀는", "그녀가", "그녀를", "그녀의", "그녀에게", "그녀한테",
]

THEY_KO_MARKERS = [
    "그들은", "그들이", "그들을", "그들의", "그들에게", "그들한테",
]

IMPLICIT_FIRST_PERSON_KO_TERMS = [
    "아내", "남편", "부인", "배우자", "집사람", "마누라", "와이프",
]

IMPLICIT_FIRST_PERSON_BLOCKERS = [
    "그의", "그녀의", "그들의", "그분의", "누구의", "어떤", "한", "이런", "저런", "그", "이", "저",
    "남의", "타인의", "이웃의",
]

PRONOUN_FORMS = {
    "i": {
        "subject": "I",
        "object": "me",
        "possessive": "my",
        "reflexive": "myself",
        "be_present": "I am",
        "be_present_contracted": "I'm",
        "be_past": "I was",
        "have": "I have",
        "have_contracted": "I've",
        "will": "I will",
        "will_contracted": "I'll",
        "would": "I would",
        "would_contracted": "I'd",
        "can": "I can",
        "do": "I do",
        "does_negation": "I do not",
    },
    "he": {
        "subject": "He",
        "object": "him",
        "possessive": "his",
        "reflexive": "himself",
        "be_present": "He is",
        "be_present_contracted": "He's",
        "be_past": "He was",
        "have": "He has",
        "have_contracted": "He's",
        "will": "He will",
        "will_contracted": "He'll",
        "would": "He would",
        "would_contracted": "He'd",
        "can": "He can",
        "do": "He does",
        "does_negation": "He does not",
    },
    "she": {
        "subject": "She",
        "object": "her",
        "possessive": "her",
        "reflexive": "herself",
        "be_present": "She is",
        "be_present_contracted": "She's",
        "be_past": "She was",
        "have": "She has",
        "have_contracted": "She's",
        "will": "She will",
        "will_contracted": "She'll",
        "would": "She would",
        "would_contracted": "She'd",
        "can": "She can",
        "do": "She does",
        "does_negation": "She does not",
    },
    "they": {
        "subject": "They",
        "object": "them",
        "possessive": "their",
        "reflexive": "themselves",
        "be_present": "They are",
        "be_present_contracted": "They're",
        "be_past": "They were",
        "have": "They have",
        "have_contracted": "They've",
        "will": "They will",
        "will_contracted": "They'll",
        "would": "They would",
        "would_contracted": "They'd",
        "can": "They can",
        "do": "They do",
        "does_negation": "They do not",
    },
}

@dataclass
class TranslationContext:
    subject: str = ENV.CONTEXT_SUBJECT
    pronoun: str = ENV.CONTEXT_PRONOUN
    narration_mode: str = ENV.CONTEXT_MODE
    last_english: Optional[str] = None

    def subject_line(self) -> str:
        subject = (self.subject or ENV.CONTEXT_SUBJECT).strip()
        pronoun = (self.pronoun or ENV.CONTEXT_PRONOUN).strip()
        pronoun_display = f"{pronoun}/him" if pronoun.lower().startswith("he") else pronoun
        return f"Main character: {subject} ({pronoun_display}). Keep all dropped subjects anchored to this reference."

    def mode_line(self) -> str:
        mode = (self.narration_mode or ENV.CONTEXT_MODE).strip()
        return (
            f"Narration mode: {mode}. "
            "Do not switch to first-person narration unless the Korean clause explicitly contains first-person markers "
            "(나, 난, 내가, 나를, 저, 제가, 저를, 우리, 우리는, 우리를) or implies speaker ownership via kinship terms "
            "(아내, 남편, 부인, 배우자)."
        )

    def previous_line(self) -> str:
        prev = (self.last_english or "").strip()
        return prev if prev else "(none yet)"

def _build_messages(ko: str, ctx: TranslationContext) -> list[dict[str, str]]:
    ko_clause = ko.strip()
    system_text = "\n".join([
        "You translate Korean sermon clauses into conservative, faithful English for live interpretation.",
        "Be concise, accurate, and avoid embellishing or implying inappropriate content.",
        ctx.subject_line(),
        ctx.mode_line(),
        "When Korean omits the subject, continue using the same subject/pronoun stated above.",
        "Never output first-person pronouns (I, me, we, our, myself) unless the Korean clause explicitly contains first-person markers or implies speaker ownership via kinship terms.",
        "If a kinship term (아내/남편/부인/배우자/집사람/와이프) appears without an explicit possessor (그의/그녀의/그들의), assume it refers to the speaker (e.g., \"my wife\").",
        "Handle reflexives (스스로) as 'himself' / 'herself' / 'themselves' according to the subject.",
        "NEVER use 'one', 'a person', 'someone', 'people', or other indefinite/generic terms as the subject unless the Korean explicitly introduces a new, unnamed entity. When Korean omits the subject, always carry the established subject forward.",
        "Output only the final English sentence.",
    ])
    user_text = "\n\n".join([
        f"Previous English sentence: {ctx.previous_line()}",
        f"IMPORTANT: The subject of this clause is \"{ctx.subject}\" ({ctx.pronoun}). Do NOT introduce a new subject or use \"one\", \"people\", \"a person\", or other generic terms unless the Korean explicitly names a new entity.",
        f"Current Korean clause (finalized): {ko_clause}",
        "Return one polished English sentence that preserves tense and intent."
    ])
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

def _contains_first_person_markers(ko: str) -> bool:
    compact = re.sub(r"\s+", "", ko or "")
    for marker in FIRST_PERSON_KO_MARKERS:
        pattern = rf"(?<![가-힣]){re.escape(marker)}(?![가-힣])"
        if re.search(pattern, compact):
            return True
    return False

def _contains_implicit_first_person_kinship(ko: str) -> bool:
    compact = re.sub(r"\s+", "", ko or "")
    if not compact:
        return False
    if _detect_third_person_pronoun(compact):
        return False
    for term in IMPLICIT_FIRST_PERSON_KO_TERMS:
        idx = compact.find(term)
        if idx == -1:
            continue
        prefix = compact[max(0, idx - 6):idx]
        if any(prefix.endswith(block) for block in IMPLICIT_FIRST_PERSON_BLOCKERS):
            continue
        return True
    return False

def _detect_third_person_pronoun(ko: str) -> str:
    compact = re.sub(r"\s+", "", ko)
    if any(marker in compact for marker in SHE_KO_MARKERS):
        return "she"
    if any(marker in compact for marker in THEY_KO_MARKERS):
        return "they"
    if any(marker in compact for marker in HE_KO_MARKERS):
        return "he"
    return ""

def _normalize_pronoun(ctx: TranslationContext) -> Optional[str]:
    raw = (ctx.pronoun or ENV.CONTEXT_PRONOUN or "").strip().lower()
    if raw.startswith("i"):
        return "i"
    if raw.startswith("he"):
        return "he"
    if raw.startswith("she"):
        return "she"
    if raw.startswith("they"):
        return "they"
    return None

def _format_replacement(match: re.Match, replacement: str) -> str:
    string = match.string
    idx = match.start()
    while idx > 0 and string[idx - 1].isspace():
        idx -= 1
    sentence_start = idx == 0 or string[idx - 1] in ".!?“”\"'‘’(" 
    if sentence_start:
        return replacement
    if replacement:
        return replacement[0].lower() + replacement[1:]
    return replacement

def _enforce_subject_guardrails(en: str, ko: str, ctx: TranslationContext) -> str:
    implicit_first_person = _contains_implicit_first_person_kinship(ko)
    if _contains_first_person_markers(ko) and not implicit_first_person:
        return en

    pronoun_key = "i" if implicit_first_person else _normalize_pronoun(ctx)

    explicit = _detect_third_person_pronoun(ko)
    if explicit:
        pronoun_key = explicit
        ctx.pronoun = explicit

    # If Korean lacks first-person cues but the English output clearly names a third-person subject
    # (e.g., "the Levites") and our context is still "we", pivot the pronoun to that subject.
    if (not _contains_first_person_markers(ko)):
        subj_hint, pronoun_hint = _infer_subject_from_english(en, "", "")
        if pronoun_hint and (not pronoun_key or pronoun_key == "we"):
            pronoun_key = pronoun_hint
            ctx.pronoun = pronoun_hint
            if subj_hint:
                ctx.subject = subj_hint

    if not pronoun_key:
        return en
    forms = PRONOUN_FORMS.get(pronoun_key)
    if not forms:
        return en

    debug_tag = "[guardrail]"
    if _GUARDRAIL_DEBUG:
        print(debug_tag, "ko:", ko, "raw_en:", en, "pronoun_key:", pronoun_key)

    updated = en

    if pronoun_key == "i":
        def replace_sentence_start(text: str, pattern: str, replacement: str) -> str:
            return re.sub(
                rf"(^|[.!?]\s+)({pattern})\b",
                lambda m: f"{m.group(1)}{replacement}",
                text,
                flags=re.IGNORECASE,
            )

        updated = replace_sentence_start(updated, r"they['’]re", forms.get("be_present_contracted") or forms.get("be_present"))
        updated = replace_sentence_start(updated, r"they are", forms.get("be_present"))
        updated = replace_sentence_start(updated, r"they were", forms.get("be_past"))
        updated = replace_sentence_start(updated, r"they['’]ve", forms.get("have_contracted") or forms.get("have"))
        updated = replace_sentence_start(updated, r"they have", forms.get("have"))
        updated = replace_sentence_start(updated, r"they['’]ll", forms.get("will_contracted") or forms.get("will"))
        updated = replace_sentence_start(updated, r"they will", forms.get("will"))
        updated = replace_sentence_start(updated, r"they['’]d", forms.get("would_contracted") or forms.get("would"))
        updated = replace_sentence_start(updated, r"they would", forms.get("would"))
        updated = replace_sentence_start(updated, r"they can", forms.get("can"))
        updated = replace_sentence_start(updated, r"they", forms.get("subject"))
        updated = replace_sentence_start(updated, r"he", forms.get("subject"))
        updated = replace_sentence_start(updated, r"she", forms.get("subject"))

        updated = re.sub(r"\bby themselves\b", "by myself", updated, flags=re.IGNORECASE)
        updated = re.sub(r"\bfor themselves\b", "for myself", updated, flags=re.IGNORECASE)
        updated = re.sub(r"\bon their own\b", "on my own", updated, flags=re.IGNORECASE)

    replacements = [
        # First-person plural guardrails
        (r"\bwe['’]re\b", forms.get("be_present_contracted") or forms.get("be_present")),
        (r"\bwe are\b", forms.get("be_present")),
        (r"\bwe were\b", forms.get("be_past")),
        (r"\bwe['’]ve\b", forms.get("have_contracted") or forms.get("have")),
        (r"\bwe have\b", forms.get("have")),
        (r"\bwe['’]ll\b", forms.get("will_contracted") or forms.get("will")),
        (r"\bwe will\b", forms.get("will")),
        (r"\bwe['’]d\b", forms.get("would_contracted") or forms.get("would")),
        (r"\bwe would\b", forms.get("would")),
        (r"\bwe can\b", forms.get("can")),
        (r"\bwe do not\b", forms.get("does_negation") or forms.get("do")),
        (r"\bwe do\b", forms.get("do")),
        (r"\bwe\b", forms.get("subject")),
        (r"\bus\b", forms.get("object")),
        (r"\bour\b", forms.get("possessive")),
        (r"\bourselves\b", forms.get("reflexive")),
        (r"\bfor ourselves\b", f"for {forms['reflexive']}"),
        (r"\bby ourselves\b", f"by {forms['reflexive']}"),
        (r"\bon our own\b", f"on {forms['possessive']} own"),
        (r"\bI['’]m\b", forms.get("be_present_contracted") or forms.get("be_present")),
        (r"\bI am\b", forms.get("be_present")),
        (r"\bI was\b", forms.get("be_past")),
        (r"\bI['’]ve\b", forms.get("have_contracted") or forms.get("have")),
        (r"\bI have\b", forms.get("have")),
        (r"\bI['’]ll\b", forms.get("will_contracted") or forms.get("will")),
        (r"\bI will\b", forms.get("will")),
        (r"\bI['’]d\b", forms.get("would_contracted") or forms.get("would")),
        (r"\bI would\b", forms.get("would")),
        (r"\bI can\b", forms.get("can")),
        (r"\bI do not\b", forms.get("does_negation") or forms.get("do")),
        (r"\bI do\b", forms.get("do")),
        (r"\bI\b", forms.get("subject")),
        (r"\bme\b", forms.get("object")),
        (r"\bmy\b", forms.get("possessive")),
        (r"\bmyself\b", forms.get("reflexive")),
        (r"\bfor myself\b", f"for {forms['reflexive']}"),
        (r"\bby myself\b", f"by {forms['reflexive']}"),
        (r"\bon my own\b", f"on {forms['possessive']} own"),
    ]
    for pattern, repl in replacements:
        if not repl:
            continue
        updated = re.sub(
            pattern,
            lambda m: _format_replacement(m, repl),
            updated,
            flags=re.IGNORECASE,
        )

    if updated != en:
        print(debug_tag, "rewritten_en:", updated)
    else:
        print(debug_tag, "no change applied")

    return updated

async def translate_ko_to_en_chunk(ko: str, ctx: Optional[TranslationContext] = None) -> str:
    if not ENV.OPENAI_API_KEY:
        return ko  # fall back to echo to keep pipeline flowing
    context = ctx or TranslationContext()
    implicit_first_person = _contains_implicit_first_person_kinship(ko)
    prompt_ctx = context
    if implicit_first_person:
        prompt_ctx = TranslationContext(
            subject="the speaker",
            pronoun="I",
            narration_mode=context.narration_mode,
            last_english=context.last_english,
        )
    body = {
        "model": ENV.resolve_translation_model(),
        "messages": _build_messages(ko, prompt_ctx),
    }
    body.update(_openai_chat_options(ENV.resolve_translation_model()))
    headers = {"Authorization": f"Bearer {ENV.OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or ko)
        text = _enforce_subject_guardrails(text, ko, context)
        if ctx is not None:
            subj_hint, pronoun_hint = _infer_subject_from_english(
                text,
                context.subject or ENV.CONTEXT_SUBJECT,
                context.pronoun or ENV.CONTEXT_PRONOUN,
            )
            ctx.subject = subj_hint
            ctx.pronoun = pronoun_hint
            ctx.last_english = text
        return text
