"""The input guardrail: checks Gemini's transcript of what the caller said.

It is plain code (no extra model call), so it adds no noticeable delay. It finds:
  - a request that tries to change the assistant's rules or asks for secrets;
  - a turn that is far too long;
  - a credit card number.
Gemini hears the caller's audio itself, so nothing can be hidden from the model. Instead, in a blocked turn no
tool runs at all (the model cannot be talked into changing the calendar), and a card number is removed from
the page and the logs.
"""
import logging
import re
from dataclasses import dataclass

from ..booking.speech import DIGIT_WORDS

log = logging.getLogger("guardrail.input")

# Speech recognition writes digits as words ("four two four two"), so both forms are checked.
WRITTEN_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){10,19}(?!\d)")
SPOKEN_DIGITS = re.compile(r"\b(?:(?:%s)\b[\s,.-]*){10,19}" % "|".join(DIGIT_WORDS), re.IGNORECASE)
CARD_WORDS = re.compile(r"\b(?:card|visa|mastercard|credit|debit)\b", re.IGNORECASE)


@dataclass
class InputVerdict:
    action: str  # "allow", "block" or "redact"
    text: str  # what to give the model (already cleaned)
    reply: str = ""  # what to say instead, when blocked
    reason: str = ""


def luhn_ok(digits):
    """The check that real card numbers pass; it separates them from phone numbers."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
    return total % 10 == 0


class InputRail:
    def __init__(self, settings):
        self.settings = settings
        self.patterns = [re.compile(pattern, re.IGNORECASE) for pattern in settings.input_block_patterns]

    def check(self, text):
        text = text.strip()
        if len(text) > self.settings.input_max_chars:
            log.warning("input blocked: too long (%d characters)", len(text))
            return InputVerdict("block", "", self.settings.input_too_long_reply, "too_long")

        for pattern in self.patterns:
            if pattern.search(text):
                log.warning("input blocked: matched %r", pattern.pattern[:40])
                return InputVerdict("block", "", self.settings.input_block_reply, "injection")

        card_talk = bool(CARD_WORDS.search(text))
        redacted = WRITTEN_DIGITS.sub(lambda match: self._remove_if_card(match, card_talk), text)
        redacted = SPOKEN_DIGITS.sub(lambda match: self._remove_if_card(match, card_talk), redacted)
        if redacted != text:
            log.warning("input changed: a card number was removed")
            return InputVerdict("redact", redacted, reason="card_number")
        return InputVerdict("allow", text)

    @staticmethod
    def _remove_if_card(match, card_talk):
        """A real card number passes the Luhn check. Speech recognition can drop or mishear a digit, so when the caller
        is talking about a card, any long number goes (a phone number said in the same breath goes too)."""
        original = match.group(0)
        digits = "".join(DIGIT_WORDS.get(piece.lower(), piece) for piece in re.findall(r"\d|[a-z]+", original, re.IGNORECASE))
        if not (card_talk or (13 <= len(digits) <= 19 and luhn_ok(digits))):
            return original
        return "[card number removed]" + (" " if original[-1].isspace() else "")