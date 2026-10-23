"""Reads every setting from the .env file.

Nothing configurable (keys, model names, URLs, thresholds, business rules) is hardcoded anywhere else.
If a value is missing or has the wrong type, startup stops and lists ALL the problems at once.
"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .booking.scheduling import parse_breaks, parse_business_hours

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Your own .env wins. Any setting it does not mention falls back to the value in .env.example, so a new
# setting added to the project never stops the app from starting.
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env.example")


class ConfigError(Exception):
    """Raised when .env is missing a value or a value has the wrong type."""


class _Reader:
    """Small helper that reads typed values and remembers every problem it finds."""

    def __init__(self):
        self.problems = []

    def _raw(self, name):
        value = os.getenv(name)
        if value is None or not value.strip():
            self.problems.append(f"{name} is missing or empty")
            return None
        return value.strip()

    def text(self, name):
        value = self._raw(name)
        return value if value is not None else ""

    def integer(self, name):
        value = self._raw(name)
        if value is None:
            return 0
        try:
            return int(value)
        except ValueError:
            self.problems.append(f"{name} must be a whole number, got {value!r}")
            return 0

    def number(self, name):
        value = self._raw(name)
        if value is None:
            return 0.0
        try:
            return float(value)
        except ValueError:
            self.problems.append(f"{name} must be a number, got {value!r}")
            return 0.0

    def optional_number(self, name):
        """Blank or missing means 'off' (returns None)."""
        value = os.getenv(name)
        if value is None or not value.strip():
            return None
        try:
            return float(value.strip())
        except ValueError:
            self.problems.append(f"{name} must be a number or left empty, got {value!r}")
            return None

    def optional_integer(self, name):
        """Blank or missing means 'let the service decide' (returns None)."""
        value = os.getenv(name)
        if value is None or not value.strip():
            return None
        try:
            return int(value.strip())
        except ValueError:
            self.problems.append(f"{name} must be a whole number or left empty, got {value!r}")
            return None

    def optional_choice(self, name, choices):
        """Blank or missing means 'let the service decide' (returns None)."""
        value = (os.getenv(name) or "").strip()
        if not value:
            return None
        if value not in choices:
            self.problems.append(f"{name} must be one of {', '.join(choices)} or left empty, got {value!r}")
            return None
        return value

    def optional_list(self, name, separator="|"):
        """Blank or missing means 'off' (an empty list)."""
        return [part.strip() for part in (os.getenv(name) or "").split(separator) if part.strip()]

    def flag(self, name):
        value = self._raw(name)
        if value is None:
            return False
        if value.lower() in ("true", "1", "yes"):
            return True
        if value.lower() in ("false", "0", "no"):
            return False
        self.problems.append(f"{name} must be true or false, got {value!r}")
        return False

    def number_list(self, name):
        value = self._raw(name)
        if value is None:
            return []
        try:
            return [int(part) for part in value.split(",")]
        except ValueError:
            self.problems.append(f"{name} must look like 50,80,120,160, got {value!r}")
            return []

    def text_list(self, name, separator=","):
        value = self._raw(name)
        if value is None:
            return []
        return [part.strip() for part in value.split(separator) if part.strip()]

    def regex_list(self, name):
        """Regular expressions separated by ;; and checked right away so a typo is found at startup."""
        patterns = self.text_list(name, separator=";;")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                self.problems.append(f"{name} has a bad pattern {pattern!r}: {exc}")
        return patterns

    def reply_template(self, name):
        """A spoken sentence with {service}, {when} and {reference} blanks. Checked now, so a typo shows up at startup."""
        text = self.text(name)
        try:
            text.format(service="x", when="y", reference="z")
        except (KeyError, IndexError, ValueError) as exc:
            self.problems.append(f"{name} may only use the blanks {{service}}, {{when}} and {{reference}} ({type(exc).__name__}: {exc})")
        return text

    def file_text(self, name):
        value = self._raw(name)
        if value is None:
            return ""
        path = PROJECT_ROOT / value
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            self.problems.append(f"{name}: cannot read {path} ({exc.strerror})")
            return ""

    def services(self, name):
        text = self.file_text(name)
        if not text:
            return []
        try:
            services = json.loads(text)
            seen = set()
            for service in services:
                if not isinstance(service["id"], str) or not service["name"] or int(service["duration_minutes"]) < 5:
                    raise ValueError("every service needs an id, a name and a duration of 5 minutes or more")
                if service["id"] in seen:
                    raise ValueError(f"the service id {service['id']!r} appears twice")
                seen.add(service["id"])
            if not services:
                raise ValueError("the list is empty")
            return services
        except (ValueError, KeyError, TypeError) as exc:
            self.problems.append(f"{name}: the services file is not valid ({exc})")
            return []


@dataclass(frozen=True)
class Settings:
    # server
    host: str
    port: int
    log_level: str

    # Google Calendar
    google_calendar_id: str
    google_calendar_api_token: str  # a secret: never log it, never show it to the model
    google_calendar_base_url: str
    calendar_timeout_seconds: float
    calendar_max_retries: int
    calendar_cache_seconds: float
    calendar_keepalive_expiry_seconds: float
    calendar_warm_interval_seconds: float

    # the business
    business_name: str
    business_staff_name: str
    business_timezone: str
    business_hours_text: str
    business_hours: dict
    business_breaks_text: str
    business_breaks: list
    services: list
    booking_buffer_minutes: int
    min_notice_minutes: int
    max_advance_days: int
    slot_step_minutes: int
    max_slots_returned: int
    max_search_days: int
    cancellation_notice_hours: int
    booking_ref_digits: int
    max_verify_failures: int
    verify_lock_seconds: float
    write_race_window_seconds: float

    # Gemini Live (hears, thinks and speaks)
    gemini_api_key: str  # a secret: never log it, never show it to the model
    gemini_live_ws_url: str
    gemini_live_model: str
    gemini_voice: str
    gemini_input_sample_rate: int
    gemini_output_sample_rate: int
    live_connect_timeout_seconds: float
    live_silence_duration_ms: int
    live_prefix_padding_ms: Optional[int]
    live_start_sensitivity: Optional[str]
    live_end_sensitivity: Optional[str]
    live_transcript_wait_ms: float

    # OpenAI (LLM)
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    openai_max_output_tokens: int
    openai_keepalive_expiry_seconds: float
    openai_warm_interval_seconds: float
    system_prompt_template: str
    history_max_messages: int
    max_tool_rounds: int
    openai_prime_on_connect: bool
    tool_timeout_seconds: float
    agent_fallback_reply: str

    # confirmation before any change
    confirm_words: List[str]
    refuse_words: List[str]
    confirm_ttl_seconds: float
    confirm_heard_share: float
    read_back_min_share: float
    quick_answer_max_words: int
    quick_extra_words: List[str]
    reply_declined: str

    # spoken while a tool works
    tool_fillers: dict  # tool name -> the phrases to say while it works
    tool_filler_prepare: List[str]
    tool_filler_write_delay_seconds: float

    # what is said when a change has been made
    reply_booked: str
    reply_already_booked: str
    reply_rescheduled: str
    reply_cancelled: str

    # guardrails
    input_max_chars: int
    input_block_patterns: List[str]
    input_block_reply: str
    input_too_long_reply: str
    output_block_reply: str
    output_unconfirmed_reply: str

    # Deepgram (STT, Flux)
    deepgram_api_key: str
    deepgram_ws_url: str
    deepgram_model: str
    deepgram_encoding: str
    deepgram_sample_rate: int
    deepgram_eot_threshold: float
    deepgram_eager_eot_threshold: Optional[float]
    deepgram_eot_timeout_ms: int
    deepgram_confirm_eot_threshold: Optional[float]  # used only for the answer to a confirmation question
    deepgram_confirm_eot_timeout_ms: int
    stt_lag_warning_ms: float

    # ElevenLabs (TTS)
    elevenlabs_api_key: str
    elevenlabs_ws_base_url: str
    elevenlabs_voice_id: str
    elevenlabs_model: str
    elevenlabs_output_format: str
    tts_sample_rate: int  # worked out from the output format, e.g. pcm_16000 -> 16000
    elevenlabs_inactivity_timeout: int
    elevenlabs_chunk_schedule: List[int]
    elevenlabs_stability: float
    elevenlabs_similarity: float
    elevenlabs_speaker_boost: bool
    elevenlabs_auto_mode: bool
    tts_connect_timeout_seconds: float
    tts_check_on_connect: bool
    tts_check_timeout_seconds: float
    tts_min_ms_per_character: float
    no_audio_warning: str
    turn_timeout_seconds: float

    # text chunking
    first_chunk_chars: int
    chunk_target_chars: int
    chunk_max_extra_chars: int

    # browser audio
    mic_chunk_ms: int

    # "I heard you": said when the caller has gone quiet but Deepgram has not finished yet
    listen_ack_phrases: List[str]
    listen_ack_silence_ms: float
    listen_ack_min_speech_ms: float
    local_speech_rms: float

    # behaviour
    backchannel_words: frozenset  # "yeah", "okay"... said while the assistant talks: no interruption
    backchannel_max_words: int
    barge_in_enabled: bool
    idle_timeout_seconds: float
    drain_grace_seconds: float
    slow_event_warning_ms: float


def load_settings():
    r = _Reader()

    output_format = r.text("ELEVENLABS_OUTPUT_FORMAT")
    tts_rate = 0
    if output_format:
        if output_format.startswith("pcm_") and output_format[4:].isdigit():
            tts_rate = int(output_format[4:])
        else:
            r.problems.append(
                f"ELEVENLABS_OUTPUT_FORMAT must be a pcm format like pcm_16000, got {output_format!r}"
            )

    hours_text = r.text("BUSINESS_HOURS")
    hours = {}
    if hours_text:
        try:
            hours = parse_business_hours(hours_text)
        except ValueError as exc:
            r.problems.append(f"BUSINESS_HOURS is not valid: {exc}")

    breaks_text = r.text("BUSINESS_BREAKS")
    breaks = []
    if breaks_text:
        try:
            breaks = parse_breaks(breaks_text)
        except ValueError as exc:
            r.problems.append(f"BUSINESS_BREAKS is not valid: {exc}")

    timezone = r.text("BUSINESS_TIMEZONE")
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            r.problems.append(f"BUSINESS_TIMEZONE {timezone!r} is not a known time zone (try Asia/Karachi)")

    search = r.text_list("TOOL_FILLER_SEARCH", separator="|")
    book, move, cancel = (r.text_list(name, separator="|") for name in ("TOOL_FILLER_BOOK", "TOOL_FILLER_RESCHEDULE", "TOOL_FILLER_CANCEL"))
    fillers = {
        "get_services": search, "get_business_config": search, "check_calendar_availability": search, "find_available_slots": search,
        "get_booking": r.text_list("TOOL_FILLER_LOOKUP", separator="|"),
        "create_booking": book, "reschedule_booking": move, "cancel_booking": cancel,
    }

    settings = Settings(
        host=r.text("HOST"),
        port=r.integer("PORT"),
        log_level=r.text("LOG_LEVEL"),
        google_calendar_id=r.text("GOOGLE_CALENDAR_ID"),
        google_calendar_api_token=r.text("GOOGLE_CALENDAR_API_TOKEN"),
        google_calendar_base_url=r.text("GOOGLE_CALENDAR_BASE_URL").rstrip("/"),
        calendar_timeout_seconds=r.number("CALENDAR_TIMEOUT_SECONDS"),
        calendar_max_retries=r.integer("CALENDAR_MAX_RETRIES"),
        calendar_cache_seconds=r.number("CALENDAR_CACHE_SECONDS"),
        calendar_keepalive_expiry_seconds=r.number("CALENDAR_KEEPALIVE_EXPIRY_SECONDS"),
        calendar_warm_interval_seconds=r.number("CALENDAR_WARM_INTERVAL_SECONDS"),
        business_name=r.text("BUSINESS_NAME"),
        business_staff_name=r.text("BUSINESS_STAFF_NAME"),
        business_timezone=timezone,
        business_hours_text=hours_text,
        business_hours=hours,
        business_breaks_text=breaks_text,
        business_breaks=breaks,
        services=r.services("SERVICES_FILE"),
        booking_buffer_minutes=r.integer("BOOKING_BUFFER_MINUTES"),
        min_notice_minutes=r.integer("MIN_NOTICE_MINUTES"),
        max_advance_days=r.integer("MAX_ADVANCE_DAYS"),
        slot_step_minutes=r.integer("SLOT_STEP_MINUTES"),
        max_slots_returned=r.integer("MAX_SLOTS_RETURNED"),
        max_search_days=r.integer("MAX_SEARCH_DAYS"),
        cancellation_notice_hours=r.integer("CANCELLATION_NOTICE_HOURS"),
        booking_ref_digits=r.integer("BOOKING_REF_DIGITS"),
        max_verify_failures=r.integer("MAX_VERIFY_FAILURES"),
        verify_lock_seconds=r.number("VERIFY_LOCK_SECONDS"),
        write_race_window_seconds=r.number("WRITE_RACE_WINDOW_SECONDS"),
        gemini_api_key=r.text("GEMINI_API_KEY"),
        gemini_live_ws_url=r.text("GEMINI_LIVE_WS_URL"),
        gemini_live_model=r.text("GEMINI_LIVE_MODEL"),
        gemini_voice=r.text("GEMINI_VOICE"),
        gemini_input_sample_rate=r.integer("GEMINI_INPUT_SAMPLE_RATE"),
        gemini_output_sample_rate=r.integer("GEMINI_OUTPUT_SAMPLE_RATE"),
        live_connect_timeout_seconds=r.number("LIVE_CONNECT_TIMEOUT_SECONDS"),
        live_silence_duration_ms=r.integer("LIVE_SILENCE_DURATION_MS"),
        live_prefix_padding_ms=r.optional_integer("LIVE_PREFIX_PADDING_MS"),
        live_start_sensitivity=r.optional_choice("LIVE_START_SENSITIVITY", ("START_SENSITIVITY_HIGH", "START_SENSITIVITY_LOW")),
        live_end_sensitivity=r.optional_choice("LIVE_END_SENSITIVITY", ("END_SENSITIVITY_HIGH", "END_SENSITIVITY_LOW")),
        live_transcript_wait_ms=r.number("LIVE_TRANSCRIPT_WAIT_MS"),
        openai_api_key=r.text("OPENAI_API_KEY"),
        openai_base_url=r.text("OPENAI_BASE_URL"),
        openai_model=r.text("OPENAI_MODEL"),
        openai_max_output_tokens=r.integer("OPENAI_MAX_OUTPUT_TOKENS"),
        openai_keepalive_expiry_seconds=r.number("OPENAI_KEEPALIVE_EXPIRY_SECONDS"),
        openai_warm_interval_seconds=r.number("OPENAI_WARM_INTERVAL_SECONDS"),
        system_prompt_template=r.file_text("SYSTEM_PROMPT_FILE"),
        history_max_messages=r.integer("HISTORY_MAX_MESSAGES"),
        max_tool_rounds=r.integer("MAX_TOOL_ROUNDS"),
        openai_prime_on_connect=r.flag("OPENAI_PRIME_ON_CONNECT"),
        tool_timeout_seconds=r.number("TOOL_TIMEOUT_SECONDS"),
        agent_fallback_reply=r.text("AGENT_FALLBACK_REPLY"),
        confirm_words=[word.lower() for word in r.text_list("CONFIRM_WORDS")],
        refuse_words=[word.lower() for word in r.text_list("REFUSE_WORDS")],
        confirm_ttl_seconds=r.number("CONFIRM_TTL_SECONDS"),
        confirm_heard_share=r.number("CONFIRM_HEARD_SHARE"),
        read_back_min_share=r.number("READ_BACK_MIN_SHARE"),
        quick_answer_max_words=r.integer("QUICK_ANSWER_MAX_WORDS"),
        quick_extra_words=[word.lower() for word in r.text_list("QUICK_ANSWER_EXTRA_WORDS")],
        reply_declined=r.text("REPLY_DECLINED"),
        tool_fillers=fillers,
        tool_filler_prepare=r.text_list("TOOL_FILLER_PREPARE", separator="|"),
        tool_filler_write_delay_seconds=r.number("TOOL_FILLER_WRITE_DELAY_SECONDS"),
        reply_booked=r.reply_template("REPLY_BOOKED"),
        reply_already_booked=r.reply_template("REPLY_ALREADY_BOOKED"),
        reply_rescheduled=r.reply_template("REPLY_RESCHEDULED"),
        reply_cancelled=r.reply_template("REPLY_CANCELLED"),
        input_max_chars=r.integer("INPUT_MAX_CHARS"),
        input_block_patterns=r.regex_list("INPUT_BLOCK_PATTERNS"),
        input_block_reply=r.text("INPUT_BLOCK_REPLY"),
        input_too_long_reply=r.text("INPUT_TOO_LONG_REPLY"),
        output_block_reply=r.text("OUTPUT_BLOCK_REPLY"),
        output_unconfirmed_reply=r.text("OUTPUT_UNCONFIRMED_REPLY"),
        deepgram_api_key=r.text("DEEPGRAM_API_KEY"),
        deepgram_ws_url=r.text("DEEPGRAM_WS_URL"),
        deepgram_model=r.text("DEEPGRAM_MODEL"),
        deepgram_encoding=r.text("DEEPGRAM_ENCODING"),
        deepgram_sample_rate=r.integer("DEEPGRAM_SAMPLE_RATE"),
        deepgram_eot_threshold=r.number("DEEPGRAM_EOT_THRESHOLD"),
        deepgram_eager_eot_threshold=r.optional_number("DEEPGRAM_EAGER_EOT_THRESHOLD"),
        deepgram_eot_timeout_ms=r.integer("DEEPGRAM_EOT_TIMEOUT_MS"),
        deepgram_confirm_eot_threshold=r.optional_number("DEEPGRAM_CONFIRM_EOT_THRESHOLD"),
        deepgram_confirm_eot_timeout_ms=r.integer("DEEPGRAM_CONFIRM_EOT_TIMEOUT_MS"),
        stt_lag_warning_ms=r.number("STT_LAG_WARNING_MS"),
        elevenlabs_api_key=r.text("ELEVENLABS_API_KEY"),
        elevenlabs_ws_base_url=r.text("ELEVENLABS_WS_BASE_URL"),
        elevenlabs_voice_id=r.text("ELEVENLABS_VOICE_ID"),
        elevenlabs_model=r.text("ELEVENLABS_MODEL"),
        elevenlabs_output_format=output_format,
        tts_sample_rate=tts_rate,
        elevenlabs_inactivity_timeout=r.integer("ELEVENLABS_INACTIVITY_TIMEOUT"),
        elevenlabs_chunk_schedule=r.number_list("ELEVENLABS_CHUNK_SCHEDULE"),
        elevenlabs_stability=r.number("ELEVENLABS_STABILITY"),
        elevenlabs_similarity=r.number("ELEVENLABS_SIMILARITY"),
        elevenlabs_speaker_boost=r.flag("ELEVENLABS_SPEAKER_BOOST"),
        elevenlabs_auto_mode=r.flag("ELEVENLABS_AUTO_MODE"),
        tts_connect_timeout_seconds=r.number("TTS_CONNECT_TIMEOUT_SECONDS"),
        tts_check_on_connect=r.flag("TTS_CHECK_ON_CONNECT"),
        tts_check_timeout_seconds=r.number("TTS_CHECK_TIMEOUT_SECONDS"),
        tts_min_ms_per_character=r.number("TTS_MIN_MS_PER_CHARACTER"),
        no_audio_warning=r.text("NO_AUDIO_WARNING"),
        turn_timeout_seconds=r.number("TURN_TIMEOUT_SECONDS"),
        first_chunk_chars=r.integer("FIRST_CHUNK_CHARS"),
        chunk_target_chars=r.integer("CHUNK_TARGET_CHARS"),
        chunk_max_extra_chars=r.integer("CHUNK_MAX_EXTRA_CHARS"),
        mic_chunk_ms=r.integer("MIC_CHUNK_MS"),
        listen_ack_phrases=r.optional_list("LISTEN_ACK_PHRASES"),
        listen_ack_silence_ms=r.number("LISTEN_ACK_SILENCE_MS"),
        listen_ack_min_speech_ms=r.number("LISTEN_ACK_MIN_SPEECH_MS"),
        local_speech_rms=r.number("LOCAL_SPEECH_RMS"),
        backchannel_words=frozenset(word.lower() for word in r.optional_list("BACKCHANNEL_WORDS", separator=",")),
        backchannel_max_words=r.integer("BACKCHANNEL_MAX_WORDS"),
        barge_in_enabled=r.flag("BARGE_IN_ENABLED"),
        idle_timeout_seconds=r.number("IDLE_TIMEOUT_SECONDS"),
        drain_grace_seconds=r.number("DRAIN_GRACE_SECONDS"),
        slow_event_warning_ms=r.number("SLOW_EVENT_WARNING_MS"),
    )

    # extra sanity checks, only once every value was read without problems
    if not r.problems:
        if any(value < 50 for value in settings.elevenlabs_chunk_schedule):
            r.problems.append("ELEVENLABS_CHUNK_SCHEDULE values must all be 50 or more (ElevenLabs rejects smaller ones)")
        if not 20 <= settings.mic_chunk_ms <= 250:
            r.problems.append("MIC_CHUNK_MS should be between 20 and 250 (80 is recommended for Flux)")
        if settings.deepgram_eager_eot_threshold is not None:
            if not 0.3 <= settings.deepgram_eager_eot_threshold <= 0.9:
                r.problems.append("DEEPGRAM_EAGER_EOT_THRESHOLD must be between 0.3 and 0.9, or empty to switch it off")
            elif settings.deepgram_eager_eot_threshold > settings.deepgram_eot_threshold:
                r.problems.append("DEEPGRAM_EAGER_EOT_THRESHOLD must not be higher than DEEPGRAM_EOT_THRESHOLD")
        confirm = settings.deepgram_confirm_eot_threshold
        if confirm is not None:
            if not 0.5 <= confirm <= 1.0:
                r.problems.append("DEEPGRAM_CONFIRM_EOT_THRESHOLD must be between 0.5 and 1.0, or empty to switch it off")
            elif settings.deepgram_eager_eot_threshold is not None and settings.deepgram_eager_eot_threshold > confirm:
                r.problems.append("DEEPGRAM_EAGER_EOT_THRESHOLD must not be higher than DEEPGRAM_CONFIRM_EOT_THRESHOLD")
            if not 500 <= settings.deepgram_confirm_eot_timeout_ms <= 60000:
                r.problems.append("DEEPGRAM_CONFIRM_EOT_TIMEOUT_MS must be between 500 and 60000")
        if not (0 < settings.confirm_heard_share <= 1 and 0 < settings.read_back_min_share <= 1):
            r.problems.append("CONFIRM_HEARD_SHARE and READ_BACK_MIN_SHARE must be between 0 and 1")
        if not 100 <= settings.live_silence_duration_ms <= 5000:
            r.problems.append("LIVE_SILENCE_DURATION_MS should be between 100 and 5000 (Google recommends 500 to 800)")
        if settings.gemini_input_sample_rate != 16000 or settings.gemini_output_sample_rate != 24000:
            r.problems.append("GEMINI_INPUT_SAMPLE_RATE must be 16000 and GEMINI_OUTPUT_SAMPLE_RATE 24000 (what Gemini Live uses)")
        if settings.deepgram_encoding != "linear16":
            r.problems.append("DEEPGRAM_ENCODING must be linear16 (the browser sends 16-bit PCM)")
        if not 4 <= settings.booking_ref_digits <= 9:
            r.problems.append("BOOKING_REF_DIGITS must be between 4 and 9")
        if settings.slot_step_minutes < 5 or settings.max_slots_returned < 1 or settings.max_search_days < 1:
            r.problems.append("SLOT_STEP_MINUTES must be 5 or more, MAX_SLOTS_RETURNED and MAX_SEARCH_DAYS at least 1")
        if settings.max_tool_rounds < 1:
            r.problems.append("MAX_TOOL_ROUNDS must be at least 1")

    if r.problems:
        raise ConfigError("\n".join(f"  - {problem}" for problem in r.problems))
    return settings


try:
    settings = load_settings()
except ConfigError as error:
    raise SystemExit(f"\nYour .env file has problems:\n{error}\n\nCompare it with .env.example and try again.\n")