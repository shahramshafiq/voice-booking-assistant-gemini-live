"""Tests for the eight booking tools against a fake Google Calendar (no internet, no API keys)."""
import asyncio
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from app.booking.calendar_client import CalendarClient
from app.booking.models import (
    BookingLookupArgs, CheckAvailabilityArgs, CreateBookingArgs, CustomerDetails, DateRange, FindSlotsArgs, RescheduleArgs,
)
from app.booking.service import BookingService
from app.config import settings as base_settings
from tests.fake_google import FakeGoogleCalendar

TZ = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 10, 19, 8, 0, tzinfo=TZ)  # Monday 8 AM
PHONE = "0300 1234567"


def make(token="ya29.fake-test-token", **changes):
    fake = FakeGoogleCalendar()
    config = replace(base_settings, calendar_cache_seconds=30, calendar_max_retries=1, **changes)
    config = replace(config, google_calendar_api_token=token)
    client = CalendarClient(config, transport=fake.transport())
    return fake, BookingService(config, client, clock=lambda: NOW)


def run(coro):
    return asyncio.run(coro)


def slot_args(service="consultation", start="tomorrow", end=None, pref=None):
    return FindSlotsArgs(service_id=service, date_range=DateRange(start_date=start, end_date=end), time_preference=pref)


def book_args(start, end, name="Ali Raza", phone=PHONE, service="consultation"):
    return CreateBookingArgs(customer_details=CustomerDetails(name=name, phone=phone), service_id=service, start_time=start, end_time=end)


def first_slot(svc, **kwargs):
    result = run(svc.find_available_slots(slot_args(**kwargs)))
    assert result["ok"] and result["slots"], result
    return result["slots"][0]


def book(svc, slot, **kwargs):
    return run(svc.create_booking(book_args(slot["start"], slot["end"], **kwargs)))


def test_services_and_business_config():
    _, service = make()
    services = run(service.get_services())["services"]
    assert [s["id"] for s in services] == ["consultation", "follow_up", "extended_consultation"]
    config = run(service.get_business_config())
    assert config["timezone"] == "Asia/Karachi" and config["cancellation_notice_hours"] == 24


def test_slots_respect_hours_breaks_and_existing_events():
    fake, service = make()
    fake.add_event("2026-10-20T14:00:00+05:00", "2026-10-20T15:00:00+05:00")  # tomorrow 2 to 3 PM is taken
    result = run(service.find_available_slots(slot_args(pref="afternoon")))
    starts = [s["start"][11:16] for s in result["slots"]]
    assert result["service"]["duration_minutes"] == 60
    assert "12:00" in starts            # fits just before the break
    assert "14:00" not in starts and "13:30" not in starts and "14:30" not in starts and "15:00" not in starts
    assert "15:30" in starts or "16:00" in starts
    assert all(s["label"].startswith("tomorrow at") for s in result["slots"])
    assert "requested_time_free" not in result  # no exact time was asked for
    assert run(service.find_available_slots(slot_args(pref="11 AM")))["requested_time_free"] is True
    assert run(service.find_available_slots(slot_args(pref="2 PM")))["requested_time_free"] is False  # taken


def test_low_level_availability_tool():
    _, service = make()
    result = run(service.check_calendar_availability(CheckAvailabilityArgs(date="tomorrow", duration=30, preferred_time="after 3 PM")))
    assert result["ok"] and result["slots"][0]["start"].startswith("2026-10-20T15:00")


def test_empty_range_looks_further_ahead():
    fake, service = make()
    fake.add_event("2026-10-20T00:00:00+05:00", "2026-10-21T00:00:00+05:00")  # all of tomorrow is booked
    result = run(service.find_available_slots(slot_args(pref="afternoon", start="tomorrow", end="tomorrow")))
    assert result["ok"] and result["slots"] == []
    assert result["next_available"] and result["next_available"][0]["start"].startswith("2026-10-21")


def test_vague_input_is_a_clean_error():
    _, service = make()
    result = run(service.find_available_slots(slot_args(start="someday")))
    assert result["ok"] is False and result["error"] == "INVALID_INPUT"
    result = run(service.find_available_slots(slot_args(service="massage")))
    assert result["error"] == "UNKNOWN_SERVICE"


def test_booking_creates_one_event_with_a_reference():
    fake, service = make()
    slot = first_slot(service)
    result = book(service, slot)
    assert result["ok"] and result["already_booked"] is False
    assert len(result["booking_reference"]) == 6 and result["booking_reference"].isdigit()
    assert len(fake.events) == 1
    event = next(iter(fake.events.values()))
    assert event["extendedProperties"]["private"]["customerPhoneKey"] == "3001234567"
    assert event["summary"] == "Consultation: Ali Raza"


def test_retrying_the_same_request_does_not_duplicate():
    fake, service = make()
    slot = first_slot(service)
    first = book(service, slot)
    second = book(service, slot, phone="+92 300 1234567")  # same person, written differently
    assert second["ok"] and second["already_booked"] is True
    assert second["booking_reference"] == first["booking_reference"]
    assert len(fake.events) == 1


def test_a_lost_answer_does_not_lose_or_duplicate_the_booking():
    fake, service = make()
    slot = first_slot(service)
    fake.fail_next = [(0, True)]  # the next request is applied, but the answer never arrives
    result = book(service, slot)
    assert result["ok"] is True
    assert len(fake.events) == 1


def test_a_taken_slot_is_rejected_with_alternatives():
    fake, service = make()
    slot = first_slot(service)
    assert book(service, slot)["ok"]
    other = book(service, slot, name="Sara Khan", phone="0311 7654321")
    assert other["ok"] is False and other["error"] == "SLOT_UNAVAILABLE" and other["reason"] == "CONFLICT"
    assert other["alternatives"]
    assert all(a["start"] != slot["start"] for a in other["alternatives"])
    assert len(fake.events) == 1


def test_availability_is_rechecked_live_before_booking():
    fake, service = make()
    slot = first_slot(service)  # this answer is now cached
    fake.add_event(slot["start"], slot["end"])  # somebody else books it directly in Google Calendar
    result = book(service, slot)
    assert result["ok"] is False and result["error"] == "SLOT_UNAVAILABLE"


def test_invalid_booking_inputs():
    _, service = make()
    slot = first_slot(service)
    wrong_length = run(service.create_booking(book_args(slot["start"], first_slot(service, service="follow_up")["end"])))
    assert wrong_length["error"] == "INVALID_INPUT"
    assert book(service, slot, phone="12")["error"] == "INVALID_INPUT"
    assert book(service, slot, name="1234")["error"] == "INVALID_INPUT"
    assert book(service, slot, service="massage")["error"] == "UNKNOWN_SERVICE"
    too_soon = run(service.create_booking(book_args("2026-10-19T08:30:00+05:00", "2026-10-19T09:30:00+05:00")))
    assert too_soon["error"] == "SLOT_UNAVAILABLE" and too_soon["reason"] == "TOO_SOON"
    too_early = run(service.create_booking(book_args("2026-10-20T07:00:00+05:00", "2026-10-20T08:00:00+05:00")))
    assert too_early["error"] == "SLOT_UNAVAILABLE" and too_early["reason"] == "OUTSIDE_HOURS"


def test_lookup_needs_the_right_reference_and_phone():
    _, service = make()
    ref = book(service, first_slot(service))["booking_reference"]
    good = run(service.get_booking(BookingLookupArgs(booking_reference=ref, customer_phone="03001234567")))
    assert good["ok"] and good["customer_name"] == "Ali Raza" and good["service"] == "Consultation"
    spaced = run(service.get_booking(BookingLookupArgs(booking_reference=" ".join(ref), customer_phone="+92-300-1234567")))
    assert spaced["ok"]
    wrong_phone = run(service.get_booking(BookingLookupArgs(booking_reference=ref, customer_phone="03009999999")))
    wrong_ref = run(service.get_booking(BookingLookupArgs(booking_reference="000000", customer_phone="03001234567")))
    assert wrong_phone["error"] == wrong_ref["error"] == "BOOKING_NOT_FOUND"
    assert wrong_phone["message"] == wrong_ref["message"]  # never reveals which of the two was wrong


def test_reschedule_updates_the_same_event():
    fake, service = make()
    slot = first_slot(service)  # tomorrow 9:00
    ref = book(service, slot)["booking_reference"]
    event_id = next(iter(fake.events))
    moved = run(service.reschedule_booking(RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="tomorrow", new_time="9:30 am")))
    assert moved["ok"] and moved["start"].startswith("2026-10-20T09:30")  # overlaps its own old slot: allowed
    assert list(fake.events) == [event_id]  # same event, not a new one
    assert fake.events[event_id]["start"]["dateTime"].startswith("2026-10-20T09:30")


def test_reschedule_to_a_taken_time_keeps_the_original():
    fake, service = make()
    ref = book(service, first_slot(service))["booking_reference"]
    fake.add_event("2026-10-21T10:00:00+05:00", "2026-10-21T11:00:00+05:00")
    before = dict(next(iter(fake.events.values()))["start"])
    result = run(service.reschedule_booking(RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="2026-10-21", new_time="10:30")))
    assert result["ok"] is False and result["error"] == "SLOT_UNAVAILABLE" and result["alternatives"]
    booking_event = [e for e in fake.events.values() if e.get("extendedProperties")][0]
    assert booking_event["start"] == before


def test_reschedule_with_a_vague_time_offers_choices_and_changes_nothing():
    fake, service = make()
    ref = book(service, first_slot(service))["booking_reference"]
    result = run(service.reschedule_booking(RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="friday", new_time="afternoon")))
    assert result["error"] == "CHOOSE_A_TIME" and result["slots"]
    assert next(iter(fake.events.values()))["start"]["dateTime"].startswith("2026-10-20")


def test_a_failed_reschedule_update_keeps_the_original():
    fake, service = make()
    ref = book(service, first_slot(service))["booking_reference"]
    original = dict(next(iter(fake.events.values()))["start"])
    fake.fail_always["PATCH"] = 500  # looking up and checking work, only the update fails
    result = run(service.reschedule_booking(RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="tomorrow", new_time="11:00")))
    assert result["ok"] is False and result["error"] == "CALENDAR_UNAVAILABLE"
    assert "not been changed" in result["message"]
    assert [m for m, _ in fake.requests].count("PATCH") == 2  # tried once and retried once
    assert next(iter(fake.events.values()))["start"] == original


def test_cancel_with_enough_notice_removes_the_event():
    fake, service = make()
    ref = book(service, first_slot(service, start="friday"))["booking_reference"]  # more than 24 hours away
    result = run(service.cancel_booking(BookingLookupArgs(booking_reference=ref, customer_phone=PHONE)))
    assert result["ok"] and result["cancelled"] is True
    assert fake.events == {}


def test_cancel_inside_the_notice_window_is_refused():
    fake, service = make()
    ref = book(service, first_slot(service))["booking_reference"]  # tomorrow 9:00 is about 25 hours away at the frozen time
    service.settings = replace(service.settings, cancellation_notice_hours=48)
    result = run(service.cancel_booking(BookingLookupArgs(booking_reference=ref, customer_phone=PHONE)))
    assert result["ok"] is False and result["error"] == "CANCELLATION_WINDOW"
    assert len(fake.events) == 1


def test_expired_token_is_a_clear_error_not_a_false_success():
    fake, service = make(token="ya29.expired")
    result = run(service.find_available_slots(slot_args()))
    assert result["ok"] is False and result["error"] == "CALENDAR_AUTH_FAILED"
    assert "ya29" not in str(result)  # the token is never shown
    slot = {"start": "2026-10-20T09:00:00+05:00", "end": "2026-10-20T10:00:00+05:00"}
    assert book(service, slot)["error"] == "CALENDAR_AUTH_FAILED"
    assert fake.events == {}


def test_changes_ask_google_for_a_short_reply_and_send_no_notifications():
    fake, service = make()
    slot = first_slot(service)
    ref = book(service, slot)["booking_reference"]
    run(service.reschedule_booking(RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="tomorrow", new_time="11:00")))
    run(service.cancel_booking(BookingLookupArgs(booking_reference=ref, customer_phone=PHONE)))
    changes = [(method, query) for (method, _), query in zip(fake.requests, fake.queries) if method in ("POST", "PATCH", "DELETE")]
    assert [m for m, _ in changes] == ["POST", "PATCH", "DELETE"]
    assert all(q.get("sendUpdates") == "none" for _, q in changes)
    assert all(q.get("fields") == "id" for m, q in changes if m != "DELETE")


def test_a_short_calendar_hiccup_is_retried_and_a_long_one_is_reported():
    fake, service = make()
    fake.fail_next = [(503, False)]
    assert run(service.find_available_slots(slot_args()))["ok"] is True  # the retry worked
    service.calendar._cache.clear()  # forget the calendar window, so the next question really has to ask Google
    fake.fail_next = [(503, False), (503, False)]
    result = run(service.check_calendar_availability(CheckAvailabilityArgs(date="friday", duration=30)))
    assert result["ok"] is False and result["error"] == "CALENDAR_UNAVAILABLE"


def test_repeated_questions_are_answered_from_memory():
    fake, service = make()
    run(service.find_available_slots(slot_args(pref="morning")))
    calls = len(fake.requests)
    run(service.find_available_slots(slot_args(pref="afternoon")))  # same day: no second request
    assert len(fake.requests) == calls


def test_confirmation_questions_are_exact_and_stable():
    _, service = make()
    slot = first_slot(service)
    plan1 = run(service.prepare("create_booking", book_args(slot["start"], slot["end"])))
    plan2 = run(service.prepare("create_booking", book_args(slot["start"], slot["end"], name="ali raza", phone="+92 300 1234567")))
    assert plan1.key == plan2.key  # the same booking, said differently, is the same action
    assert "Consultation tomorrow at 9 AM" in plan1.question and "Ali Raza" in plan1.question and "0 3 0, 0 1 2" in plan1.question
    other_name = run(service.prepare("create_booking", book_args(slot["start"], slot["end"], name="Ali Raza Khan")))
    assert other_name.key != plan1.key  # a different name needs its own confirmation
    later = first_slot(service, pref="after 3 PM")
    assert run(service.prepare("create_booking", book_args(later["start"], later["end"]))).key != plan1.key


def test_preparing_changes_nothing_and_applying_does():
    fake, service = make()
    slot = first_slot(service)
    plan = run(service.prepare("create_booking", book_args(slot["start"], slot["end"])))
    assert plan.result is None and plan.apply is not None
    assert fake.events == {} and "POST" not in [m for m, _ in fake.requests]  # only reads so far
    result = run(plan.apply())
    assert result["ok"] and len(fake.events) == 1


def test_two_callers_cannot_both_get_one_slot():
    fake, service = make()
    slot = first_slot(service)
    ali = run(service.prepare("create_booking", book_args(slot["start"], slot["end"], name="Ali Raza", phone="0300 1234567")))
    sara = run(service.prepare("create_booking", book_args(slot["start"], slot["end"], name="Sara Khan", phone="0311 7654321")))
    assert ali.apply and sara.apply  # both looked at the calendar while the slot was still free
    assert run(ali.apply())["ok"]
    late = run(sara.apply())
    assert late["ok"] is False and late["error"] == "SLOT_UNAVAILABLE"
    assert len(fake.events) == 1


def test_the_reference_cannot_be_worked_out_and_a_retry_by_someone_else_learns_nothing():
    fake, service = make()
    slot = first_slot(service)
    first = book(service, slot)
    intruder = book(service, slot, name="Mallory")  # knows the phone number, the service and the time
    assert intruder["ok"] is False and intruder["error"] == "SLOT_UNAVAILABLE"
    assert "Ali" not in str(intruder) and first["booking_reference"] not in str(intruder)
    cancelled = run(service.cancel_booking(BookingLookupArgs(booking_reference=first["booking_reference"], customer_phone=PHONE)))
    assert cancelled["ok"] is True and fake.events == {}  # the slot is about 25 hours away: enough notice
    again = book(service, slot)
    assert again["ok"] and again["booking_reference"] != first["booking_reference"]  # random, not derived from the booking


def test_a_booking_that_was_moved_can_be_booked_again_at_the_old_time():
    fake, service = make()
    slot = first_slot(service)
    ref = book(service, slot)["booking_reference"]
    run(service.reschedule_booking(RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="tomorrow", new_time="2 pm")))
    again = book(service, slot)
    assert again["ok"] and again["already_booked"] is False and "9 AM" in again["label"]
    assert len(fake.events) == 2


def test_wrong_tries_lock_one_phone_number_for_everyone():
    _, service = make()
    for guess in ("111111", "222222", "333333"):
        result = run(service.get_booking(BookingLookupArgs(booking_reference=guess, customer_phone=PHONE)))
        assert result["error"] == "BOOKING_NOT_FOUND"
    locked = run(service.get_booking(BookingLookupArgs(booking_reference="444444", customer_phone="+92 300 1234567")))
    assert locked["error"] == "TOO_MANY_ATTEMPTS"
    other = run(service.get_booking(BookingLookupArgs(booking_reference="444444", customer_phone="0311 7654321")))
    assert other["error"] == "BOOKING_NOT_FOUND"  # another number is not locked


def test_a_live_reschedule_check_reads_the_booking_and_the_new_day_at_the_same_time():
    fake, service = make()
    ref = book(service, first_slot(service))["booking_reference"]
    real_list = service.calendar.list_events
    reading = {"now": 0, "most": 0}

    async def slow_list(*args, **kwargs):  # every trip to Google takes 0.2 s
        reading["now"] += 1
        reading["most"] = max(reading["most"], reading["now"])
        await asyncio.sleep(0.2)
        reading["now"] -= 1
        return await real_list(*args, **kwargs)

    service.calendar.list_events = slow_list
    args = RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="tomorrow", new_time="11:00")
    plan = run(service.prepare("reschedule_booking", args, fresh=True))
    assert plan.result is None and "11 AM" in plan.question
    assert reading["most"] == 2  # both reads were on their way to Google together


def test_a_reschedule_whose_answer_was_lost_is_not_reported_as_unchanged():
    fake, service = make()
    ref = book(service, first_slot(service))["booking_reference"]
    plan = run(service.prepare("reschedule_booking", RescheduleArgs(booking_reference=ref, customer_phone=PHONE, new_date="tomorrow", new_time="11:00")))
    fake.fail_next = [(0, True), (0, True)]  # the update is made both times, but no answer ever arrives
    result = run(plan.apply())
    assert result["ok"] is True and result["start"].startswith("2026-10-20T11:00")
