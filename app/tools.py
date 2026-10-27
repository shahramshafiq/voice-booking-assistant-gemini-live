"""The tools Gemini may call, and the gate every call must pass.

Gemini never touches the calendar. It asks, and ToolRunner decides:
  - the input is checked against the schema (bad input comes back as a clear error);
  - READ tools run at once, because they change nothing;
  - WRITE tools (create, reschedule, cancel) only run if the caller said "yes" to this exact action in their answer
    right after hearing the question. Otherwise the action is parked as "pending" and the result carries the
    read-back question, written by the code, for Gemini to read out word for word;
  - in a turn the input guardrail blocked, no tool runs at all.
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from pydantic import ValidationError

from .booking.calendar_client import CalendarError
from .booking.models import (
    BookingLookupArgs, CheckAvailabilityArgs, CreateBookingArgs, FindSlotsArgs, NoArgs, RescheduleArgs,
)
from .booking.service import BookingError
from .booking.speech import spell_digits

log = logging.getLogger("tools")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    model: type
    description: str
    write: bool = False


TOOLS = {spec.name: spec for spec in [
    ToolSpec("get_services", NoArgs, "List the services with id, name and duration. The prompt already has them; rarely needed."),
    ToolSpec("get_business_config", NoArgs, "The staff member, calendar, hours, breaks, buffer and booking rules. Rarely needed."),
    ToolSpec("check_calendar_availability", CheckAvailabilityArgs,
             "Low level: free times on ONE day for a length in minutes. Prefer find_available_slots."),
    ToolSpec("find_available_slots", FindSlotsArgs,
             "Find free appointment times for a service over one or more days. Use this to offer times."),
    ToolSpec("create_booking", CreateBookingArgs,
             "Book an appointment. Call it as soon as you have the service, the time, the name and the phone number, "
             "without asking the caller first. The result's say field is the confirmation question: read it out loud "
             "word for word and wait for the answer. Do NOT call any tool until the caller answers. After the yes, "
             "call it again with the same details.", write=True),
    ToolSpec("get_booking", BookingLookupArgs, "Look up a booking by reference and phone number."),
    ToolSpec("reschedule_booking", RescheduleArgs,
             "Move a booking to a new day and time. Call it as soon as you have the new time, without asking the caller "
             "first. Read the say field of the result out loud word for word and wait for the answer. Do NOT call any "
             "tool until the caller answers. After the yes, call it again with the same details.",
             write=True),
    ToolSpec("cancel_booking", BookingLookupArgs,
             "Cancel a booking. Call it as soon as the caller asks to cancel, without asking them first. Read the say "
             "field of the result out loud word for word and wait for the answer. Do NOT call any tool until the "
             "caller answers. After the yes, call it again with the same details.", write=True),
]}

NEEDS_VERIFICATION = {"get_booking", "reschedule_booking", "cancel_booking"}


@dataclass
class ToolContext:
    """What a tool call needs to know about the turn it belongs to."""
    emit: Callable  # async function that sends a small event to the browser
    turn_no: int = 0
    blocked: bool = False  # the input guardrail blocked what the caller said in this turn


def _gemini_schema(schema, definitions):
    """Pydantic's JSON Schema in the plain form Gemini accepts: nested models written in place, an optional field
    (anyOf with null) as just its type, no titles or defaults, and the type names in capitals (STRING, OBJECT),
    as Gemini's Schema type documents them."""
    if isinstance(schema, list):
        return [_gemini_schema(item, definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        return _gemini_schema(definitions[schema["$ref"].split("/")[-1]], definitions)
    if "anyOf" in schema:
        kept = [option for option in schema["anyOf"] if option.get("type") != "null"]
        if len(kept) == 1:
            merged = {**kept[0], **{k: v for k, v in schema.items() if k == "description"}}
            return _gemini_schema(merged, definitions)
    cleaned = {k: _gemini_schema(v, definitions) for k, v in schema.items() if k not in ("title", "$defs", "default")}
    if isinstance(cleaned.get("type"), str):
        cleaned["type"] = cleaned["type"].upper()
    return cleaned


def build_definitions():
    """The tool list in the format Gemini expects (functionDeclarations), generated from the same classes that
    validate the input."""
    definitions = []
    for spec in TOOLS.values():
        declaration = {"name": spec.name, "description": spec.description}
        schema = spec.model.model_json_schema()
        if schema.get("properties"):  # a tool without input gets no parameters at all
            declaration["parameters"] = _gemini_schema(schema, schema.get("$defs", {}))
        definitions.append(declaration)
    return definitions


def for_model(result):
    """What Gemini gets back. Internal fields stay out; the say sentence stays in, Gemini reads it out."""
    return {k: v for k, v in result.items() if k != "event_id"}


def _word_pattern(words):
    escaped = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![a-z'])(?:{escaped})(?![a-z'])") if words else None


class ToolRunner:
    """One per conversation: it knows the conversation's memory."""

    def __init__(self, settings, service, memory):
        self.settings = settings
        self.service = service
        self.memory = memory
        self.definitions = build_definitions()
        self._yes = _word_pattern(settings.confirm_words)
        self._no = _word_pattern(settings.refuse_words)

    @staticmethod
    def is_write(name):
        return name in TOOLS and TOOLS[name].write

    def _cleaned(self, text):
        return re.sub(r"[^a-z' ]", " ", text.lower().replace("\u2019", "'"))

    def is_refusal(self, text):
        return bool(self._no and self._no.search(self._cleaned(text)))

    def is_affirmative(self, text):
        """Did the caller say yes? A "no" or "wait" in the same answer cancels it."""
        return not self.is_refusal(text) and bool(self._yes and self._yes.search(self._cleaned(text)))

    def success_reply(self, tool, result):
        """The sentence to say after a change has really been made. Written by the code from the real result."""
        if tool == "create_booking":
            template = self.settings.reply_already_booked if result.get("already_booked") else self.settings.reply_booked
        elif tool == "reschedule_booking":
            template = self.settings.reply_rescheduled
        else:
            template = self.settings.reply_cancelled
        return template.format(service=result.get("service"), when=result.get("label"),
                               reference=spell_digits(str(result.get("booking_reference") or "")))

    async def caller_said_yes(self, pending):
        """Did the caller say yes in their answer to the question? Gemini can understand the answer a moment before
        its text of it reaches us, so wait up to LIVE_TRANSCRIPT_WAIT_MS for the text."""
        deadline = time.monotonic() + self.settings.live_transcript_wait_ms / 1000
        while True:
            if self.memory.pending is not pending:
                return False  # another call replaced or finished the question meanwhile
            answer = self.memory.answer_turn(pending)
            if answer is not None and self.memory.turn_no > answer:
                log.info("the caller has said something else since answering the question")
                return False
            words = self.memory.answer_to(pending)
            if self.is_affirmative(words):
                return True
            if self.is_refusal(words) or time.monotonic() >= deadline:
                log.info("no yes from the caller after the question (heard %r)", words.strip())
                return False
            await asyncio.sleep(0.05)

    # ================================================================ the one entry point
    async def run(self, name, raw_arguments, ctx):
        """raw_arguments: a dict (what Gemini sends) or a JSON string."""
        started = time.perf_counter()
        args = None
        recorded = False  # True when a finished change already updated the memory itself
        try:
            spec = TOOLS.get(name)
            if spec is None:
                result = {"ok": False, "error": "UNKNOWN_TOOL", "message": f"There is no tool called {name}."}
            elif ctx.blocked:
                result = {"ok": False, "error": "BLOCKED_BY_SAFETY",
                          "message": f"The safety filter blocked what the caller said. Do not do it. Say: {self.settings.input_block_reply}"}
            else:
                args = self._parse(spec, raw_arguments)
                result, recorded = await self._dispatch(spec, args, ctx)
        except ValueError as exc:  # bad JSON or bad fields
            result = {"ok": False, "error": "INVALID_INPUT", "message": str(exc)}
        except BookingError as exc:
            result = exc.as_result()
        except CalendarError as exc:
            result = {"ok": False, "error": exc.code, "message": exc.message}
        except asyncio.TimeoutError:
            result = {"ok": False, "error": "TOOL_TIMEOUT", "message": "That took too long. Ask the caller to try again in a moment."}
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tool %s crashed", name)  # details go to the log only, never to the model
            result = {"ok": False, "error": "INTERNAL_ERROR", "message": "Something went wrong on our side."}

        if not recorded:
            self.record(name, args, result)
        elapsed = (time.perf_counter() - started) * 1000
        log.info("tool %s -> %s (%.0f ms)", name, result.get("error", "ok"), elapsed)
        try:
            await ctx.emit({"type": "tool", "name": name, "ok": bool(result.get("ok")), "error": result.get("error"), "ms": round(elapsed)})
        except Exception:
            pass
        return result

    # ================================================================ steps
    @staticmethod
    def _parse(spec, raw_arguments):
        if isinstance(raw_arguments, dict):
            data = raw_arguments
        else:
            try:
                data = json.loads(raw_arguments) if raw_arguments and raw_arguments.strip() else {}
            except json.JSONDecodeError:
                raise ValueError("The arguments were not valid JSON.")
        try:
            return spec.model.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            raise ValueError(f"Invalid arguments ({problems}). Ask the caller for what is missing.")

    def record(self, name, args, result):
        """Write what a finished tool call taught us into the memory."""
        if args is not None:
            self.memory.note_result(name, args, result)
        if result.get("error") == "BOOKING_NOT_FOUND":
            self.memory.verify_failures += 1
        if result.get("ok"):
            self.memory.achieved.add(name)

    async def _dispatch(self, spec, args, ctx):
        """Returns (result, recorded)."""
        if spec.name in NEEDS_VERIFICATION and self.memory.verify_failures >= self.settings.max_verify_failures:
            return {"ok": False, "error": "TOO_MANY_ATTEMPTS",
                    "message": f"Too many wrong attempts. Ask the caller to contact {self.settings.business_staff_name} directly."}, False
        if not spec.write:
            method = getattr(self.service, spec.name)
            return await asyncio.wait_for(method(args), self.settings.tool_timeout_seconds), False
        return await self._run_write(spec, args, ctx)

    async def _run_write(self, spec, args, ctx):
        # Step 1: check everything against the calendar. Nothing is changed here. The confirmation QUESTION is checked
        # against the in-memory calendar window; the check right before the real change always asks Google.
        pending = self.memory.pending
        answering = pending is not None and pending.tool == spec.name and pending.heard_at is not None
        plan = await self.service.prepare(spec.name, args, self.memory.verified_booking, fresh=answering)
        if plan.result is not None:  # an error, or a retry of something already done: nothing to confirm
            return plan.result, False

        # Step 2: the real change needs a yes to exactly this action, said in the caller's answer to the question
        # (their first words after hearing it). A yes from any other turn never counts.
        pending = self.memory.pending
        confirmed = self.memory.pending_is_confirmable(plan.key) and await self.caller_said_yes(pending)
        if not confirmed:
            self.memory.set_pending(plan.key, spec.name, plan.question)
            log.info("%s is waiting for the caller's yes", spec.name)
            return {"ok": False, "error": "CONFIRMATION_REQUIRED", "say": plan.question,
                    "message": "STOP. Nothing has been done yet. Do NOT call any tool now. Read the say sentence to the "
                               "caller out loud, word for word, from its first word to its last. Then wait silently "
                               "for the caller to answer yes or no."}, False
        self.memory.pending = None

        # From here on the change must finish even if the caller interrupts. The memory is therefore
        # updated by a callback of the change itself, which runs even when nobody is waiting any more.
        change = asyncio.ensure_future(plan.apply())

        def remember(done):
            if not done.cancelled() and done.exception() is None:
                self.record(spec.name, args, done.result())

        change.add_done_callback(remember)
        result = await asyncio.shield(change)
        if result.get("ok"):
            # The code writes the announcement from the real result, so the numbers are always right.
            result["say"] = self.success_reply(spec.name, result)
            result["message"] = ("The change is done. Continue naturally from what you just said and read the say "
                                 "sentence out loud now, as a smooth continuation of the filler you just spoke.")
        return result, True
