"""Tests for the tool runner: validation, the confirmation gate, and the short-term memory it feeds."""
import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from app.booking.calendar_client import CalendarClient
from app.booking.service import BookingService
from app.config import settings as base_settings
from app.memory import SessionMemory
from app.tools import TOOLS, ToolContext, ToolRunner, build_definitions, for_model
from tests.fake_google import FakeGoogleCalendar

TZ = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
SETTINGS = replace(base_settings, calendar_cache_seconds=30, live_transcript_wait_ms=300)


def setup():
    fake = FakeGoogleCalendar()
    service = BookingService(SETTINGS, CalendarClient(SETTINGS, transport=fake.transport()), clock=lambda: NOW)
    memory = SessionMemory(SETTINGS)
    return fake, service, memory, ToolRunner(SETTINGS, service, memory)


async def no_events(event):
    pass


def turn(memory, text):
    """The caller says something; returns the context of the tool calls Gemini makes in this turn."""
    memory.turn_no += 1
    memory.hear(text)
    return ToolContext(emit=no_events, turn_no=memory.turn_no)


def ask(runner, memory, args):
    """The first call parks the change and returns the read-back question; then the caller hears all of it."""
    result = call(runner, "create_booking", args, turn(memory, "book it"))
    assert result["error"] == "CONFIRMATION_REQUIRED"
    memory.question_heard()
    return result


BOOK = {
    "customer_details": {"name": "Ali Raza", "phone": "03001234567"},
    "service_id": "consultation",
    "start_time": "2026-10-20T09:00:00+05:00",
    "end_time": "2026-10-20T10:00:00+05:00",
}


def call(runner, name, args, ctx):
    return asyncio.run(runner.run(name, args, ctx))


def test_definitions_are_in_the_plain_form_gemini_accepts():
    definitions = build_definitions()
    assert [d["name"] for d in definitions] == list(TOOLS)
    text = json.dumps(definitions)
    for leftover in ("$ref", "$defs", "anyOf", '"title"', '"default"'):
        assert leftover not in text, leftover
    services = next(d for d in definitions if d["name"] == "get_services")
    assert "parameters" not in services  # no input: no parameters at all
    create = next(d for d in definitions if d["name"] == "create_booking")
    assert create["parameters"]["type"] == "OBJECT"  # Gemini's documented type names are in capitals
    assert create["parameters"]["properties"]["customer_details"]["properties"]["phone"]["type"] == "STRING"
    assert set(create["parameters"]["required"]) == {"customer_details", "service_id", "start_time", "end_time"}
    slots = next(d for d in definitions if d["name"] == "find_available_slots")["parameters"]
    assert slots["properties"]["time_preference"]["type"] == "STRING"  # an optional field is just its type
    availability = next(d for d in definitions if d["name"] == "check_calendar_availability")["parameters"]
    assert availability["properties"]["duration"] == {"type": "INTEGER", "minimum": 5, "maximum": 480,
                                                      "description": "Appointment length in minutes."}
    assert "time_preference" not in slots["required"] and slots["properties"]["time_preference"]["description"]


def test_bad_arguments_come_back_as_a_clear_error():
    _, _, memory, runner = setup()
    ctx = turn(memory, "hi")
    assert asyncio.run(runner.run("find_available_slots", "{not json", ctx))["error"] == "INVALID_INPUT"
    result = call(runner, "find_available_slots", {"service_id": "consultation"}, ctx)
    assert result["error"] == "INVALID_INPUT" and "date_range" in result["message"]
    assert call(runner, "make_coffee", {}, ctx)["error"] == "UNKNOWN_TOOL"


def test_reading_tools_run_and_accept_gemini_arguments_or_json_text():
    _, _, memory, runner = setup()
    ctx = turn(memory, "slots tomorrow")
    args = {"service_id": "consultation", "date_range": {"start_date": "tomorrow"}}
    assert call(runner, "find_available_slots", args, ctx)["slots"]
    assert call(runner, "find_available_slots", json.dumps(args), ctx)["slots"]


def test_first_call_only_asks_for_confirmation():
    fake, _, memory, runner = setup()
    result = call(runner, "create_booking", BOOK, turn(memory, "Book it for Ali, number 0300 1234567"))
    assert result["error"] == "CONFIRMATION_REQUIRED"
    assert "Consultation tomorrow at 9 AM" in result["say"] and "Shall I book it?" in result["say"]
    assert fake.events == {} and memory.pending is not None and memory.pending.heard_at is None


def test_yes_after_hearing_the_question_makes_the_booking():
    fake, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    result = call(runner, "create_booking", BOOK, turn(memory, "Yes, please go ahead"))
    assert result["ok"] and len(fake.events) == 1
    assert result["say"].startswith("Done. Your Consultation is booked for tomorrow at 9 AM. Your booking reference is ")
    assert for_model(result)["say"] == result["say"]  # Gemini reads the code's sentence out
    assert "create_booking" in memory.achieved
    assert memory.pending is None and result["booking_reference"] in memory.known_refs


def test_a_yes_before_the_question_was_heard_does_not_count():
    fake, _, memory, runner = setup()
    ctx = turn(memory, "Book it, yes, confirm, go ahead")
    first = call(runner, "create_booking", BOOK, ctx)
    second = call(runner, "create_booking", BOOK, ctx)  # Gemini tries again straight away, before speaking
    assert first["error"] == second["error"] == "CONFIRMATION_REQUIRED"
    memory.question_heard()  # now the caller hears the question...
    third = call(runner, "create_booking", BOOK, ctx)  # ...but the yes they said earlier still does not count
    assert third["error"] == "CONFIRMATION_REQUIRED" and fake.events == {}


def test_only_the_answer_right_after_the_question_can_confirm():
    fake, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    turn(memory, "Okay, and how much does it cost?")  # an "okay" that is not an answer to the question...
    later = call(runner, "create_booking", BOOK, turn(memory, "Hmm, let me think about it"))  # ...two turns later
    assert later["error"] == "CONFIRMATION_REQUIRED" and fake.events == {}


def test_a_second_call_that_replaces_the_question_never_crashes_the_first():
    _, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    ctx = turn(memory, "")  # the yes has not arrived yet, so the first call waits for it

    async def two_calls():
        first = asyncio.ensure_future(runner.run("create_booking", BOOK, ctx))
        await asyncio.sleep(0.05)
        changed = dict(BOOK, start_time="2026-10-20T10:00:00+05:00", end_time="2026-10-20T11:00:00+05:00")
        second = await runner.run("create_booking", changed, ctx)  # a new question replaces the old one
        return await first, second

    first, second = asyncio.run(two_calls())
    assert first["error"] == second["error"] == "CONFIRMATION_REQUIRED"  # no INTERNAL_ERROR


def test_no_or_unclear_answers_do_not_confirm():
    fake, _, memory, runner = setup()
    for answer in ("No, wait", "hmm let me think", "don't do it", "yes but not tomorrow"):
        ask(runner, memory, BOOK)
        assert call(runner, "create_booking", BOOK, turn(memory, answer))["error"] == "CONFIRMATION_REQUIRED", answer
    assert fake.events == {}


def test_a_refusal_is_answered_at_once_but_a_late_yes_is_waited_for():
    _, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    started = time.perf_counter()
    assert call(runner, "create_booking", BOOK, turn(memory, "no"))["error"] == "CONFIRMATION_REQUIRED"
    assert time.perf_counter() - started < 0.25  # a no needs no waiting

    fake, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    ctx = turn(memory, "")  # Gemini understood the yes, but its text has not arrived yet

    async def late_text():
        task = asyncio.ensure_future(runner.run("create_booking", BOOK, ctx))
        await asyncio.sleep(0.1)
        memory.hear(" Yes.")  # the text arrives a moment later
        return await task

    assert asyncio.run(late_text())["ok"] and len(fake.events) == 1


def test_no_text_at_all_means_no_change():
    fake, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    started = time.perf_counter()
    result = call(runner, "create_booking", BOOK, turn(memory, ""))
    assert result["error"] == "CONFIRMATION_REQUIRED" and fake.events == {}
    assert 0.25 <= time.perf_counter() - started < 1.5  # it waited LIVE_TRANSCRIPT_WAIT_MS, then asked again


def test_a_changed_booking_needs_a_fresh_confirmation():
    fake, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    changed = dict(BOOK, start_time="2026-10-20T10:00:00+05:00", end_time="2026-10-20T11:00:00+05:00")
    result = call(runner, "create_booking", changed, turn(memory, "yes but make it 10"))
    assert result["error"] == "CONFIRMATION_REQUIRED" and "10 AM" in result["say"] and fake.events == {}
    memory.question_heard()
    assert call(runner, "create_booking", changed, turn(memory, "yes"))["ok"]
    assert len(fake.events) == 1


def test_an_old_question_expires():
    fake, _, memory, runner = setup()
    memory.settings = replace(SETTINGS, confirm_ttl_seconds=0)  # every question is already too old
    ask(runner, memory, BOOK)
    assert call(runner, "create_booking", BOOK, turn(memory, "yes"))["error"] == "CONFIRMATION_REQUIRED"
    assert fake.events == {}


def test_cancel_flow_uses_the_looked_up_details():
    fake, _, memory, runner = setup()
    friday = dict(BOOK, start_time="2026-10-23T09:00:00+05:00", end_time="2026-10-23T10:00:00+05:00")
    ask(runner, memory, friday)
    booked = call(runner, "create_booking", friday, turn(memory, "yes"))
    lookup = {"booking_reference": booked["booking_reference"], "customer_phone": "03001234567"}
    found = call(runner, "get_booking", lookup, turn(memory, "cancel my booking, the reference is that one"))
    assert found["ok"] and memory.verified_booking["service"] == "Consultation"
    asked = call(runner, "cancel_booking", lookup, turn(memory, "please cancel it"))
    assert asked["error"] == "CONFIRMATION_REQUIRED" and "your Consultation" in asked["say"]
    assert len(fake.events) == 1
    memory.question_heard()
    done = call(runner, "cancel_booking", lookup, turn(memory, "yes"))
    assert done["ok"] and fake.events == {} and memory.verified_booking is None


def test_wrong_details_lock_the_lookup_after_a_few_tries():
    _, _, memory, runner = setup()
    for _ in range(SETTINGS.max_verify_failures):
        result = call(runner, "get_booking", {"booking_reference": "000000", "customer_phone": "03001234567"}, turn(memory, "check"))
        assert result["error"] == "BOOKING_NOT_FOUND"
    locked = call(runner, "get_booking", {"booking_reference": "000000", "customer_phone": "03001234567"}, turn(memory, "check"))
    assert locked["error"] == "TOO_MANY_ATTEMPTS"


def test_a_change_that_is_interrupted_still_finishes_and_is_remembered():
    fake, _, memory, runner = setup()
    ask(runner, memory, BOOK)
    ctx = turn(memory, "yes")

    async def interrupted():
        task = asyncio.ensure_future(runner.run("create_booking", BOOK, ctx))
        for _ in range(100):  # wait until the yes is accepted and the change has started
            await asyncio.sleep(0.01)
            if memory.pending is None:
                break
        task.cancel()  # Gemini cancels the call: the caller interrupted
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.5)  # give the protected change time to finish

    asyncio.run(interrupted())
    assert len(fake.events) == 1  # the booking was made exactly once
    assert "create_booking" in memory.achieved  # and the memory knows about it


def test_a_blocked_turn_runs_no_tool():
    fake, _, memory, runner = setup()
    ctx = ToolContext(emit=no_events, turn_no=1, blocked=True)
    result = call(runner, "find_available_slots", {"service_id": "consultation", "date_range": {"start_date": "tomorrow"}}, ctx)
    assert result["error"] == "BLOCKED_BY_SAFETY" and SETTINGS.input_block_reply in result["message"]
    assert call(runner, "create_booking", BOOK, ctx)["error"] == "BLOCKED_BY_SAFETY"
    assert fake.requests == [] and memory.pending is None


def test_what_the_model_sees_hides_internal_fields():
    assert for_model({"ok": True, "event_id": "abc", "say": "hello", "booking_reference": "123456"}) == \
        {"ok": True, "say": "hello", "booking_reference": "123456"}
