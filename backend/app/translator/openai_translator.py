from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import httpx
from ..env import ENV
import re

FIRST_PERSON_KO_MARKERS = [
    "나는", "난", "내가", "내게", "나를", "나도", "나만", "나와", "나에게", "나한테",
    "저는", "전", "제가", "제게", "저를", "저도", "저만", "저와", "저에게", "저한테",
    "우리", "우리가", "우리는", "우릴", "우리의", "우리도", "우리만", "우리와", "우리에게", "우리한테"
]

PRONOUN_FORMS = {
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
            "(나, 난, 내가, 나를, 저, 제가, 저를, 우리, 우리는, 우리를)."
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
        "Never output first-person pronouns (I, me, we, our, myself) unless the Korean clause explicitly contains first-person markers.",
        "Handle reflexives (스스로) as 'himself' / 'herself' / 'themselves' according to the subject.",
        "Output only the final English sentence.",
    ])
    user_text = "\n\n".join([
        f"Previous English sentence: {ctx.previous_line()}",
        f"Subject hint: continue referring to {ctx.subject} as '{ctx.pronoun}'.",
        f"Current Korean clause (finalized): {ko_clause}",
        "Return one polished English sentence that preserves tense and intent."
    ])
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

def _contains_first_person_markers(ko: str) -> bool:
    compact = re.sub(r"\s+", "", ko)
    return any(marker in compact for marker in FIRST_PERSON_KO_MARKERS)

def _normalize_pronoun(ctx: TranslationContext) -> Optional[str]:
    raw = (ctx.pronoun or ENV.CONTEXT_PRONOUN or "").strip().lower()
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
    pronoun_key = _normalize_pronoun(ctx)
    if not pronoun_key:
        return en
    forms = PRONOUN_FORMS.get(pronoun_key)
    if not forms:
        return en

    debug_tag = "[guardrail]"
    print(debug_tag, "ko:", ko, "raw_en:", en, "pronoun_key:", pronoun_key)

    if _contains_first_person_markers(ko):
        print(debug_tag, "skip because first-person marker detected")
        return en

    updated = en

    replacements = [
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
    body = {
        "model": ENV.TRANSLATION_MODEL,
        "temperature": 0.2,
        "messages": _build_messages(ko, context),
    }
    headers = {"Authorization": f"Bearer {ENV.OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or ko)
        return _enforce_subject_guardrails(text, ko, context)
