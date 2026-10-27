"""The eight booking tools, as plain backend functions.

The model only asks; every rule lives here: opening hours, breaks, buffers, notice, duplicate
protection, the phone check and the cancellation policy. Each function returns a small dict that
always has "ok": true or false, and on a failure an "error" code and a "message" the model can
turn into a spoken sentence.
"""
import asyncio
import functools
import hashlib
import logging
import re
import secrets
import time as time_module
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from .calendar_client import CalendarError, CalendarUnavailable
from .scheduling import Rules, busy_intervals, event_bounds, free_slots, pick_slots, slot_problem
from .speech import say_slot, spell_digits, spell_phone
from .timeparse import Preference, parse_clock, parse_date, parse_date_range, parse_time_preference

log = logging.getLogger("booking")

NOT_FOUND_MESSAGE = "No booking matches that reference and phone number."


class BookingError(Exception):
    """A problem the caller can understand. It becomes {"ok": false, "error": code, "message": ...}."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_result(self):
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


@dataclass
class Plan:
    """The prepared (not yet done) version of a booking change."""
    key: Optional[str] = None  # identifies exactly this action, so a "yes" can only confirm this action
    question: Optional[str] = None  # what the caller is asked to confirm
    result: Optional[dict] = None  # set when there is nothing to confirm: an error, or something already done
    apply: Optional[Callable[[], Awaitable[dict]]] = None  # makes the change


def _guarded(method):
    """Turn every expected failure into a result dict, so a tool call never crashes the conversation."""

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except BookingError as exc:
            log.info("%s -> %s", method.__name__, exc.code)
            return exc.as_result()
        except CalendarError as exc:
            log.warning("%s -> calendar problem: %s", method.__name__, exc.code)
            return {"ok": False, "error": exc.code, "message": exc.message}

    return wrapper


def normalize_phone(text):
    digits = re.sub(r"\D", "", str(text or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if not 7 <= len(digits) <= 15:
        raise BookingError("INVALID_INPUT", "The phone number must have 7 to 15 digits. Ask the caller to repeat it.")
    return digits


def phone_key(digits):
    """The last 10 digits, so +92 300 1234567 and 0300 1234567 are the same number."""
    return digits[-10:]


def _private(event):
    return event.get("extendedProperties", {}).get("private", {})


class BookingService:
    def __init__(self, settings, calendar, clock=None):
        """`clock` is only used by the tests to freeze the time."""
        self.settings = settings
        self.calendar = calendar
        self.tz = ZoneInfo(settings.business_timezone)
        self.rules = Rules(
            tz=self.tz,
            hours=settings.business_hours,
            breaks=settings.business_breaks,
            buffer_minutes=settings.booking_buffer_minutes,
            step_minutes=settings.slot_step_minutes,
            max_advance_days=settings.max_advance_days,
        )
        self.services = {service["id"]: service for service in settings.services}
        self._clock = clock
        self._write_lock = asyncio.Lock()  # Limit: one lock for the one staff member; per-day locks if that ever limits us
        self._recent_writes = []  # (start, end, event id, time) of changes this server made a moment ago
        self._failed_lookups = {}  # phone key -> times of wrong reference/phone tries, across all callers
        self._background = set()  # keeps references to small background tasks

    def now(self):
        return self._clock() if self._clock else datetime.now(self.tz)

    # ================================================================ small helpers
    def _service(self, service_id):
        key = str(service_id).strip().lower()
        if key in self.services:
            return self.services[key]
        for service in self.services.values():  # also accept the spoken name, e.g. "follow-up"
            if service["name"].lower() == key:
                return service
        raise BookingError("UNKNOWN_SERVICE", "That service does not exist.", valid_service_ids=list(self.services))

    def _notice(self, service=None):
        extra = service.get("min_notice_minutes", 0) if service else 0
        return max(self.settings.min_notice_minutes, extra)

    def _parse_datetime(self, text):
        try:
            moment = datetime.fromisoformat(str(text).strip())
        except ValueError:
            raise BookingError("INVALID_INPUT", f"{text!r} is not a valid date and time. Use the start and end exactly as returned by find_available_slots.")
        return moment.replace(tzinfo=self.tz) if moment.tzinfo is None else moment.astimezone(self.tz)

    def _day_start(self, day):
        return datetime.combine(day, time.min, tzinfo=self.tz)

    async def _window_events(self, fresh=False):
        """Every event from today to the last bookable day, in ONE request. It is kept in memory and refreshed
        in the background (see warm), so searching for free times and looking up a booking need no trip to Google."""
        today = self.now().date()
        last = today + timedelta(days=self.settings.max_advance_days + 1)
        return await self.calendar.list_events(self._day_start(today), self._day_start(last), fresh=fresh)

    async def warm(self):
        """Refresh the in-memory window now. It also proves the token still works (a failure raises CalendarError)."""
        await self._window_events(fresh=True)

    def _rewarm_soon(self):
        """After a change, refill the window in the background so the next question is answered from memory."""
        async def refill():
            try:
                await self.warm()
            except Exception as exc:
                log.debug("background refresh failed: %s", type(exc).__name__)

        task = asyncio.ensure_future(refill())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _day_events(self, day, fresh=False):
        today = self.now().date()
        inside_window = today <= day <= today + timedelta(days=self.settings.max_advance_days)
        if fresh or not inside_window:
            return await self.calendar.list_events(self._day_start(day), self._day_start(day + timedelta(days=1)), fresh=fresh)
        start, end = self._day_start(day), self._day_start(day + timedelta(days=1))
        found = []
        for event in await self._window_events():
            bounds = event_bounds(event, self.tz)
            if bounds is not None and bounds[0] < end and bounds[1] > start:
                found.append(event)
        return found

    def _payload(self, slots):
        today = self.now().date()
        return [{"start": s.isoformat(), "end": e.isoformat(), "label": say_slot(s, today)} for s, e in slots]

    def _booking_dict(self, event):
        props = _private(event)
        start, end = event_bounds(event, self.tz)
        return {
            "booking_reference": props.get("bookingRef"),
            "event_id": event["id"],
            "service": self.services.get(props.get("serviceId"), {}).get("name", props.get("serviceId")),
            "customer_name": props.get("customerName"),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": say_slot(start, self.now().date()),
        }

    # ================================================================ tool 1 and 2: information
    @_guarded
    async def get_services(self, args=None):
        return {"ok": True, "services": [
            {k: v for k, v in service.items() if k in ("id", "name", "duration_minutes", "description", "min_notice_minutes")}
            for service in self.services.values()
        ]}

    @_guarded
    async def get_business_config(self, args=None):
        s = self.settings
        return {
            "ok": True,
            "business_name": s.business_name,
            "staff_member": s.business_staff_name,
            "calendar_id": s.google_calendar_id,
            "timezone": s.business_timezone,
            "working_hours": s.business_hours_text,
            "breaks": s.business_breaks_text,
            "booking_buffer_minutes": s.booking_buffer_minutes,
            "minimum_notice_minutes": s.min_notice_minutes,
            "maximum_advance_days": s.max_advance_days,
            "cancellation_notice_hours": s.cancellation_notice_hours,
        }

    # ================================================================ tool 3 and 4: finding slots
    async def _search(self, first_day, last_day, duration, pref, notice, exclude_id=None, look_ahead=True):
        now = self.now()
        today = now.date()
        horizon = today + timedelta(days=self.settings.max_advance_days)
        if last_day < today:
            raise BookingError("INVALID_INPUT", "That day has already passed.")
        first_day = max(first_day, today)
        if first_day > horizon:
            raise BookingError("TOO_FAR", f"Appointments can only be booked up to {self.settings.max_advance_days} days ahead.")
        last_day = min(last_day, horizon, first_day + timedelta(days=self.settings.max_search_days - 1))

        events = await self._window_events()
        busy = busy_intervals(events, self.rules, exclude_id)
        days = []
        day = first_day
        while day <= last_day:
            days.append((day, free_slots(day, duration, busy, self.rules, now, notice, pref)))
            day += timedelta(days=1)
        slots = pick_slots(days, self.settings.max_slots_returned)

        result = {"ok": True, "timezone": self.settings.business_timezone, "slots": self._payload(slots)}
        if not slots:
            result["message"] = "Nothing is free in that range."
            if look_ahead and last_day < horizon:
                later = await self._search(last_day + timedelta(days=1), horizon, duration, pref, notice, exclude_id, look_ahead=False)
                result["next_available"] = later["slots"]
        return result

    @_guarded
    async def check_calendar_availability(self, args):
        today = self.now().date()
        try:
            day = parse_date(args.date, today)
            pref = parse_time_preference(args.preferred_time)
        except ValueError as exc:
            raise BookingError("INVALID_INPUT", str(exc))
        return await self._search(day, day, args.duration, pref, self.settings.min_notice_minutes)

    @_guarded
    async def find_available_slots(self, args):
        service = self._service(args.service_id)
        today = self.now().date()
        try:
            first, last = parse_date_range(args.date_range.start_date, args.date_range.end_date, today)
            pref = parse_time_preference(args.time_preference)
        except ValueError as exc:
            raise BookingError("INVALID_INPUT", str(exc))
        result = await self._search(first, last, service["duration_minutes"], pref, self._notice(service))
        result["service"] = {"id": service["id"], "name": service["name"], "duration_minutes": service["duration_minutes"]}
        if pref is not None and pref.target is not None:
            # say it plainly: the model once told a caller that 11 AM was taken while 11 AM was in this very list
            result["requested_time_free"] = any(datetime.fromisoformat(s["start"]).time() == pref.target for s in result["slots"])
        return result

    async def _unavailable(self, problem, start, duration, notice, events, exclude_id=None):
        """Build the 'that time does not work' error, with other times the caller can pick instead."""
        messages = {
            "CONFLICT": "That time is already taken.",
            "OUTSIDE_HOURS": "That time is outside the working hours or during the break.",
            "TOO_SOON": f"Appointments need at least {notice} minutes notice.",
            "TOO_FAR": f"Appointments can only be booked up to {self.settings.max_advance_days} days ahead.",
        }
        alternatives = []
        if problem != "TOO_FAR":
            pref = Preference(target=start.timetz().replace(tzinfo=None))
            busy = busy_intervals(events, self.rules, exclude_id)
            same_day = free_slots(start.date(), duration, busy, self.rules, self.now(), notice, pref)
            alternatives = self._payload(sorted(same_day[:self.settings.max_slots_returned]))
            if not alternatives:
                later = await self._search(start.date() + timedelta(days=1), start.date() + timedelta(days=self.settings.max_search_days),
                                           duration, pref, notice, exclude_id, look_ahead=False)
                alternatives = later["slots"]
        return BookingError("SLOT_UNAVAILABLE", messages[problem] + " Offer the alternatives, if there are any.", reason=problem, alternatives=alternatives)

    # ================================================================ tool 6: look up
    async def _find_booking(self, reference_text, phone_text, fresh=False):
        """Find a booking only when the reference AND the phone number match. One generic error otherwise.
        fresh=True asks Google; otherwise the in-memory window answers (no network)."""
        digits = self.settings.booking_ref_digits
        reference = re.sub(r"\D", "", str(reference_text or ""))
        try:
            phone = normalize_phone(phone_text)
        except BookingError:
            raise BookingError("BOOKING_NOT_FOUND", NOT_FOUND_MESSAGE)
        key = phone_key(phone)

        now = time_module.monotonic()
        recent = [t for t in self._failed_lookups.get(key, []) if t > now - self.settings.verify_lock_seconds]
        self._failed_lookups[key] = recent
        if len(recent) >= self.settings.max_verify_failures:
            raise BookingError("TOO_MANY_ATTEMPTS",
                               f"Too many wrong attempts. Ask the caller to contact {self.settings.business_staff_name} directly.")

        events = []
        if len(reference) == digits:
            if fresh:
                events = await self.calendar.list_events(private_props={"bookingRef": reference, "customerPhoneKey": key}, fresh=True)
            else:
                events = [e for e in await self._window_events()
                          if _private(e).get("bookingRef") == reference and _private(e).get("customerPhoneKey") == key]
            events = [e for e in events if e.get("status") != "cancelled"]
        if not events:
            recent.append(now)
            raise BookingError("BOOKING_NOT_FOUND", NOT_FOUND_MESSAGE)
        return events[0]

    @_guarded
    async def get_booking(self, args):
        event = await self._find_booking(args.booking_reference, args.customer_phone)
        return {"ok": True, "status": "confirmed", **self._booking_dict(event)}

    # ================================================================ tools 5, 7 and 8: changes, in two steps
    # prepare() reads and checks everything against the live calendar and changes NOTHING, so it can run
    # while the caller is still finishing the sentence. plan.apply() makes the one change; whoever calls it
    # decides when (only after the caller said yes, and after the caller really finished speaking).
    async def prepare(self, tool_name, args, known=None, fresh=True):
        """Step 1 of a change. Returns a Plan:
          plan.result set      already decided (an error, or a retry of something that was already done);
          plan.key/question    what the caller must confirm, and plan.apply() to run after the yes.
        fresh=False checks against the in-memory window (used for the confirmation QUESTION); fresh=True asks
        Google (used before the real change, so the final check is always live).
        """
        try:
            return await getattr(self, f"_prepare_{tool_name}")(args, known, fresh)
        except BookingError as exc:
            log.info("%s -> %s", tool_name, exc.code)
            return Plan(result=exc.as_result())
        except CalendarError as exc:
            log.warning("%s -> calendar problem: %s", tool_name, exc.code)
            return Plan(result={"ok": False, "error": exc.code, "message": exc.message})

    @staticmethod
    def _safe(change):
        """Make Plan.apply report expected failures as result dicts, like every other tool."""

        async def run():
            try:
                return await change()
            except BookingError as exc:
                log.info("%s -> %s", change.__name__, exc.code)
                return exc.as_result()
            except CalendarError as exc:
                log.warning("%s -> calendar problem: %s", change.__name__, exc.code)
                return {"ok": False, "error": exc.code, "message": exc.message}

        return run

    async def _run_now(self, tool_name, args):
        """Prepare and apply at once, without asking anybody (used by the tests and by direct callers)."""
        plan = await self.prepare(tool_name, args)
        return plan.result if plan.result is not None else await plan.apply()

    async def create_booking(self, args):
        return await self._run_now("create_booking", args)

    async def reschedule_booking(self, args):
        return await self._run_now("reschedule_booking", args)

    async def cancel_booking(self, args):
        return await self._run_now("cancel_booking", args)

    # ---------------------------------------------------------------- two bookings racing for one slot
    def _remember_write(self, start, end, event_id):
        self._recent_writes.append((start, end, event_id, time_module.monotonic()))

    def _raced(self, start, end, read_at, own_event_id=None):
        """Did another booking made by this server take the slot after we looked at the calendar (at `read_at`)?"""
        cutoff = time_module.monotonic() - self.settings.write_race_window_seconds
        self._recent_writes = [w for w in self._recent_writes if w[3] > cutoff]
        pad = timedelta(minutes=self.settings.booking_buffer_minutes)
        return any(
            w[3] >= read_at and w[2] != own_event_id and start < w[1] + pad and end > w[0] - pad
            for w in self._recent_writes
        )

    # ---------------------------------------------------------------- create
    def _check_create_args(self, args):
        """Check everything about a new booking that can be checked without the calendar."""
        service = self._service(args.service_id)
        details = args.customer_details
        name = re.sub(r"\s+", " ", details.name).strip()
        if not re.search(r"[A-Za-z]", name):
            raise BookingError("INVALID_INPUT", "The customer name is missing. Ask for it.")
        phone = normalize_phone(details.phone)
        start = self._parse_datetime(args.start_time)
        end = self._parse_datetime(args.end_time)
        if end - start != timedelta(minutes=service["duration_minutes"]):
            raise BookingError("INVALID_INPUT", f"A {service['name']} lasts {service['duration_minutes']} minutes, so end_time must be that long after start_time.")
        idem = hashlib.sha256(f"{phone_key(phone)}|{service['id']}|{start.astimezone(timezone.utc).isoformat()}".encode()).hexdigest()[:20]
        return service, name, phone, start, end, idem

    def _new_reference(self):
        """A random number: it cannot be worked out from the phone number and the time."""
        digits = self.settings.booking_ref_digits
        return str(secrets.randbelow(10 ** digits)).zfill(digits)

    async def _prepare_create_booking(self, args, known, fresh):
        service, name, phone, start, end, idem = self._check_create_args(args)
        ref = self._new_reference()
        notice = self._notice(service)
        today = self.now().date()

        read_at = time_module.monotonic()
        events = await self._day_events(start.date(), fresh=fresh)
        for event in events:
            same_request = (
                _private(event).get("idemKey") == idem
                and event_bounds(event, self.tz)[0] == start
                and str(_private(event).get("customerName", "")).lower() == name.lower()
            )
            if same_request:  # the same request again: do not book twice (and never show it to a different name)
                log.info("duplicate create_booking ignored")
                return Plan(result={"ok": True, "already_booked": True, **self._booking_dict(event)})
        problem = slot_problem(start, end, busy_intervals(events, self.rules), self.rules, self.now(), notice)
        if problem:
            raise await self._unavailable(problem, start, service["duration_minutes"], notice, events)

        notes = (args.customer_details.notes or "").strip()
        body = {
            "summary": f"{service['name']}: {name}",
            "description": f"Booked by the voice assistant.\nCustomer: {name}\nPhone: {phone}\nReference: {ref}" + (f"\nNotes: {notes}" if notes else ""),
            "start": {"dateTime": start.isoformat(), "timeZone": self.settings.business_timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.settings.business_timezone},
            "extendedProperties": {"private": {
                "bookingRef": ref, "idemKey": idem, "serviceId": service["id"],
                "customerName": name, "customerPhone": phone, "customerPhoneKey": phone_key(phone),
            }},
        }

        async def create_booking():
            async with self._write_lock:
                if self._raced(start, end, read_at):
                    raise BookingError("SLOT_UNAVAILABLE", "That time was just taken by another booking. Search for times again.",
                                       reason="CONFLICT", alternatives=[])
                try:
                    event = await self.calendar.insert_event(body)
                except CalendarUnavailable:
                    # The answer got lost, but the event may exist. Look before saying anything.
                    events_now = await self._day_events(start.date(), fresh=True)
                    event = next((e for e in events_now if _private(e).get("idemKey") == idem and event_bounds(e, self.tz)[0] == start), None)
                    if event is None:
                        raise
                    log.warning("create_booking: the answer was lost but the event exists, reference %s", ref)
                self._remember_write(start, end, event["id"])
            self._rewarm_soon()
            log.info("booked %s, reference %s", service["id"], ref)
            created = {"id": event["id"], "start": body["start"], "end": body["end"], "extendedProperties": body["extendedProperties"]}
            return {"ok": True, "already_booked": False, **self._booking_dict(created)}

        key = f"create|{phone_key(phone)}|{service['id']}|{start.astimezone(timezone.utc).isoformat()}|{name.lower()}"
        question = (f"Just to confirm: a {service['name']} {say_slot(start, today)}, for {name}, "
                    f"phone number {spell_phone(phone)}. Shall I book it?")
        return Plan(key=key, question=question, apply=self._safe(create_booking))

    # ---------------------------------------------------------------- reschedule
    def _parse_date_or_error(self, text):
        try:
            return parse_date(text, self.now().date())
        except ValueError as exc:
            raise BookingError("INVALID_INPUT", str(exc))

    async def _prepare_reschedule_booking(self, args, known, fresh):
        day = self._parse_date_or_error(args.new_date)
        # The booking and the new day are read at the same time: after the caller's yes (fresh=True) each one is a
        # trip to Google, and the caller is waiting.
        read_at = time_module.monotonic()
        event, events = await asyncio.gather(
            self._find_booking(args.booking_reference, args.customer_phone, fresh=fresh),
            self._day_events(day, fresh=fresh),
            return_exceptions=True,
        )
        for outcome in (event, events):
            if isinstance(outcome, BaseException):
                raise outcome
        old_start, old_end = event_bounds(event, self.tz)
        if old_start < self.now():
            raise BookingError("PAST_BOOKING", "That appointment has already taken place.")
        duration = int((old_end - old_start).total_seconds() // 60)
        notice = self._notice(self.services.get(_private(event).get("serviceId")))
        today = self.now().date()

        try:
            new_start = datetime.combine(day, parse_clock(args.new_time), tzinfo=self.tz)
        except ValueError:
            # "afternoon" is not a time: show what is free instead of moving anything
            try:
                pref = parse_time_preference(args.new_time)
            except ValueError as exc:
                raise BookingError("INVALID_INPUT", str(exc))
            found = await self._search(day, day, duration, pref, notice, exclude_id=event["id"], look_ahead=False)
            raise BookingError("CHOOSE_A_TIME", "Nothing was changed. Offer these times and ask which one the caller wants.", slots=found["slots"])
        new_end = new_start + timedelta(minutes=duration)
        if new_start == old_start:
            raise BookingError("NO_CHANGE", "The appointment is already at that time.")

        problem = slot_problem(new_start, new_end, busy_intervals(events, self.rules, event["id"]), self.rules, self.now(), notice)
        if problem:
            raise await self._unavailable(problem, new_start, duration, notice, events, exclude_id=event["id"])

        async def reschedule_booking():
            async with self._write_lock:
                if self._raced(new_start, new_end, read_at, own_event_id=event["id"]):
                    raise BookingError("SLOT_UNAVAILABLE", "That time was just taken by another booking. Search for times again.",
                                       reason="CONFLICT", alternatives=[])
                try:
                    await self.calendar.patch_event(event["id"], {
                        "start": {"dateTime": new_start.isoformat(), "timeZone": self.settings.business_timezone},
                        "end": {"dateTime": new_end.isoformat(), "timeZone": self.settings.business_timezone},
                    })
                except CalendarUnavailable as exc:
                    # the answer got lost: look at the calendar before saying that nothing changed
                    now_event = await self._find_booking(args.booking_reference, args.customer_phone, fresh=True)
                    if event_bounds(now_event, self.tz)[0] != new_start:
                        raise BookingError(exc.code, exc.message + " The original appointment has not been changed.")
                    log.warning("reschedule_booking: the answer was lost but the change was made")
                except CalendarError as exc:
                    raise BookingError(exc.code, exc.message + " The original appointment has not been changed.")
                self._remember_write(new_start, new_end, event["id"])
            self._rewarm_soon()
            log.info("rescheduled booking %s", _private(event).get("bookingRef"))
            moved = dict(event, start={"dateTime": new_start.isoformat()}, end={"dateTime": new_end.isoformat()})
            return {"ok": True, **self._booking_dict(moved)}

        reference = _private(event)["bookingRef"]
        about = f"your {known['service']} {known['label']}" if known and known.get("booking_reference") == reference else f"booking {spell_digits(reference)}"
        key = f"reschedule|{reference}|{_private(event).get('customerPhoneKey')}|{new_start.isoformat()}"
        question = f"Just to confirm: you want to move {about} to {say_slot(new_start, today)}. Shall I go ahead?"
        return Plan(key=key, question=question, apply=self._safe(reschedule_booking))

    # ---------------------------------------------------------------- cancel
    async def _prepare_cancel_booking(self, args, known, fresh):
        event = await self._find_booking(args.booking_reference, args.customer_phone, fresh=fresh)
        start, _ = event_bounds(event, self.tz)
        hours_left = (start - self.now()).total_seconds() / 3600
        if hours_left < 0:
            raise BookingError("PAST_BOOKING", "That appointment has already taken place.")
        if hours_left < self.settings.cancellation_notice_hours:
            raise BookingError(
                "CANCELLATION_WINDOW",
                f"Appointments need {self.settings.cancellation_notice_hours} hours notice to cancel, so it cannot be cancelled here. "
                f"Ask the caller to contact {self.settings.business_staff_name} directly.",
            )

        async def cancel_booking():
            await self.calendar.delete_event(event["id"])
            self._rewarm_soon()
            log.info("cancelled booking %s", _private(event).get("bookingRef"))
            return {"ok": True, "cancelled": True, **self._booking_dict(event)}

        reference = _private(event)["bookingRef"]
        about = f"your {known['service']} {known['label']}" if known and known.get("booking_reference") == reference else f"booking {spell_digits(reference)}"
        key = f"cancel|{reference}|{_private(event).get('customerPhoneKey')}"
        return Plan(key=key, question=f"Just to confirm: you want to cancel {about}. Is that right?", apply=self._safe(cancel_booking))