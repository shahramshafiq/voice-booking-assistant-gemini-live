"""Short-term memory for ONE conversation. It starts empty and is thrown away when the call ends.

Gemini keeps the conversation itself (what was said and answered). This memory holds what the CODE must know
for sure. It is written from tool calls and results and from Gemini's transcript of the caller, never by the model:
  - an action that is waiting for the caller's "yes", and the moment the caller had heard the whole question;
  - what the caller said and when (a "yes" only counts if it came after the question was heard);
  - the booking references that really appeared and the tools that really succeeded (the output guardrail checks
    the assistant's claims against them), the booking that was looked up, and the wrong tries.
"""
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class Pending:
    """An action the caller has been asked to confirm."""
    key: str  # identifies exactly this action
    tool: str
    question: str  # what is read back to the caller
    turn_no: int  # the conversation turn in which it was asked
    created: float
    heard_at: Optional[float] = None  # when the caller had heard the whole question; before that a yes cannot count


class SessionMemory:
    def __init__(self, settings):
        self.settings = settings
        self.turn_no = 0  # counts the caller's turns
        self.pending = None
        self.verify_failures = 0  # wrong reference/phone attempts in this conversation
        self.known_refs = set()  # booking references that really appeared in this conversation
        self.achieved = set()  # tools that really succeeded in this conversation (the output guardrail checks claims against it)
        self.verified_booking = None
        self.caller_words = []  # [(time, text)]: Gemini's transcript of the caller, piece by piece

    # ---------------------------------------------------------------- what the caller said
    # The caller's words and the moment the question was heard use the fine-grained clock, because a word said just
    # before that moment must never count as an answer to the question.
    def hear(self, text):
        now = time.perf_counter()
        self.caller_words.append((now, text))
        too_old = now - self.settings.confirm_ttl_seconds  # older words can never confirm anything
        while self.caller_words and self.caller_words[0][0] < too_old:
            self.caller_words.pop(0)

    def words_since(self, moment):
        return "".join(text for at, text in self.caller_words if at >= moment)

    # ---------------------------------------------------------------- the confirmation step
    def set_pending(self, key, tool, question):
        self.pending = Pending(key, tool, question, self.turn_no, time.monotonic())

    def question_heard(self):
        """The caller has now heard the whole confirmation question (or nearly all of it before they answered)."""
        if self.pending is not None and self.pending.heard_at is None:
            self.pending.heard_at = time.perf_counter()

    def pending_is_confirmable(self, key):
        """True when `key` is the action the caller was asked about, they heard the question, and it is not too old."""
        p = self.pending
        return (
            p is not None
            and p.key == key
            and p.heard_at is not None
            and time.monotonic() - p.created < self.settings.confirm_ttl_seconds
        )

    # ---------------------------------------------------------------- facts from tool calls
    def note_result(self, tool, args, result):
        """Remember what a tool call taught us."""
        reference = result.get("booking_reference") or getattr(args, "booking_reference", None)
        if reference:
            self.known_refs.add("".join(ch for ch in str(reference) if ch.isdigit()))
        if not result.get("ok"):
            return
        if tool in ("get_booking", "create_booking", "reschedule_booking"):
            self.verified_booking = {key: result.get(key) for key in ("booking_reference", "service", "label", "customer_name")}
        if tool == "cancel_booking":
            self.verified_booking = None
