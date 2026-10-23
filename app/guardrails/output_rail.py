"""The output guardrail: checks what the assistant is saying, from Gemini's own transcript of its answer.

Gemini makes its sound faster than the browser plays it, so this text normally arrives before the caller hears
the same words, and a stopped answer is usually silenced before the bad part is played. It is plain code, so it
adds no noticeable delay. It finds:
  - secrets (API keys, the calendar token) being read out;
  - the assistant's own instructions being read out;
  - a claim like "I have booked it" when no booking change really succeeded in this conversation;
  - a booking reference, said after the word "reference", that no tool ever returned (a made-up number).
"""
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("guardrail.output")

SECRET_PATTERNS = [
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),  # a Google API key, like the Gemini key
    re.compile(r"ya29\.[A-Za-z0-9._-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{12,}", re.IGNORECASE),
]

BOOKED = {"create_booking", "reschedule_booking", "get_booking"}  # a looked-up booking may also be called confirmed


def _claim(verbs):
    """'I've booked it', 'Your Consultation for Wednesday at 3 PM is now booked', 'It has been booked'.
    'That time is already booked' (meaning taken) is not a claim and passes."""
    done = rf"(?:'s|is|are|was|has been|have been)\s+(?:now\s+|all\s+|successfully\s+)?(?:{verbs})\b"
    return re.compile(
        rf"\b(?:i(?:'ve| have)|we(?:'ve| have))\s+(?:now\s+|just\s+|already\s+|successfully\s+)?(?:{verbs})\b"
        rf"|\b(?:your|the)\s+(?:appointment|booking|consultation|follow-up|session)\b[^.?!]{{0,60}}?\b{done}"
        rf"|\b(?:it|that|everything)\s*{done}",
        re.IGNORECASE,
    )


CLAIMS = [
    (_claim("booked|confirmed|scheduled"), BOOKED),
    (re.compile(r"\byou(?:'re| are) (?:booked|confirmed)\b|\bbooking (?:is )?confirmed\b", re.IGNORECASE), BOOKED),
    (_claim("cancell?ed"), {"cancel_booking"}),
    (_claim("rescheduled|moved"), {"reschedule_booking"}),
    (re.compile(r"\byou(?:'re| are) all set\b", re.IGNORECASE), {"create_booking", "reschedule_booking", "cancel_booking"}),
]

# A leak repeats the instructions word for word, so this many words in a row from the rules counts as one.
# Fewer words catch normal replies too ("I can only help with appointments with Shahram Shafiq" shares 7 with the rules).
LEAK_WORDS = 10

REFERENCE_MENTION = re.compile(r"\b(?:reference|ref)\b[^0-9]{0,25}(\d(?:[ ,.-]{0,2}\d)*)", re.IGNORECASE)


@dataclass
class OutputVerdict:
    ok: bool
    text: str  # the text itself when ok, otherwise the replacement to say
    reason: str = ""


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


class OutputRail:
    def __init__(self, settings, instructions):
        self.settings = settings
        self.secrets = [value for value in (settings.gemini_api_key, settings.google_calendar_api_token) if len(value) >= 8]
        # only the RULES part of the prompt is protected; the business information is meant to be spoken
        rules = _words(instructions.split("BUSINESS INFORMATION")[0])
        self.rule_phrases = {tuple(rules[i:i + LEAK_WORDS]) for i in range(len(rules) - LEAK_WORDS + 1)}

    def check(self, phrase, tail, achieved, known_refs):
        """`tail` is the end of the previous phrase, so a sentence cut in two is still checked as one.
        `achieved` is the set of tools that really succeeded in this conversation."""
        joined = f"{tail} {phrase}".replace("\u2019", "'").strip()

        if any(secret in phrase for secret in self.secrets) or any(p.search(phrase) for p in SECRET_PATTERNS):
            return self._block("secret", self.settings.output_block_reply)

        words = _words(joined)
        if any(tuple(words[i:i + LEAK_WORDS]) in self.rule_phrases for i in range(len(words) - LEAK_WORDS + 1)):
            return self._block("prompt_leak", self.settings.output_block_reply)

        for pattern, needed in CLAIMS:
            if pattern.search(joined) and not (needed & achieved):
                return self._block("unconfirmed_claim", self.settings.output_unconfirmed_reply)

        for number in self._mentioned_references(joined):
            if number not in known_refs:
                return self._block("unknown_reference", self.settings.output_unconfirmed_reply)

        return OutputVerdict(True, phrase)

    # ---------------------------------------------------------------- helpers
    def _block(self, reason, replacement):
        log.warning("output blocked: %s", reason)
        return OutputVerdict(False, replacement, reason)

    def _mentioned_references(self, text):
        """Numbers said right after the word 'reference' (also one digit at a time, '4 8 2 1 7 6') that have a reference's length."""
        runs = (re.sub(r"\D", "", m.group(1)) for m in REFERENCE_MENTION.finditer(text))
        return [run for run in runs if len(run) == self.settings.booking_ref_digits]