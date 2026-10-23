<div align="center">

# Voice Booking Assistant (Gemini Live)

A voice receptionist that books, looks up, moves and cancels appointments in Google Calendar, built on the Gemini Live API for low latency.

![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Gemini Live](https://img.shields.io/badge/Gemini%20Live-gemini--3.8--live-8E75B2?style=flat-square&logo=googlegemini&logoColor=white)
![Google Calendar](https://img.shields.io/badge/Google%20Calendar-API%20v3-4285F4?style=flat-square&logo=googlecalendar&logoColor=white)
![Tests](https://img.shields.io/badge/tests-121%20passing-brightgreen?style=flat-square)

</div>

## Overview

You open a web page, press Start and talk. The assistant hears you, decides what you need, checks one Google Calendar through a set of tools, offers free times, and only changes the calendar after you say yes to a read-back of the exact details.

This is the Gemini Live version of [voice-booking-assistant](https://github.com/shahramshafiq/voice-booking-assistant). That project chained three streaming services (speech to text, a language model, text to speech). Here one Gemini Live WebSocket hears the audio, decides when you have finished, calls the tools and speaks the answer, so there are no hand-offs between services. The booking rules, the confirmation step and the guardrails stay in the backend code.

## How It Works

```text
browser microphone (16 kHz PCM)                         Google Calendar API
        |                                                        ^
        v                                                        |
  FastAPI WebSocket  --audio-->  Gemini Live  --tool call-->  tool runner
  (VoiceSession)    <--audio--  (one socket)  <--result----  (rules, confirmation gate)
        |
        v
browser speakers (24 kHz PCM), transcripts, tool list, latency table
```

1. The page connects to the server when it loads, and the server opens a Gemini Live session with the instructions and the eight tools, so pressing Start has nothing left to wait for.
2. Microphone audio goes to Gemini in 40 ms pieces. Gemini detects the end of your turn itself.
3. When Gemini asks for a tool, the backend checks the input, runs the booking rules against the calendar and answers with the result.
4. Gemini's sound is passed to the browser as it arrives. Its transcript of its own words is checked by the output guardrail on the way.

## Key Features

- **Eight booking tools:** services, business config, calendar availability, slot search, create, look up, reschedule and cancel. All rules (opening hours, breaks, buffers, notice, durations, cancellation policy) live in backend code and `.env`, never in the model.
- **Confirm before any change:** the first call to a change tool only parks the action and returns a read-back question written by the code. Gemini reads it out. The change happens only when the caller says yes in their answer right after hearing that question. A yes from any other turn, a question that was cut short, or a read-back that Gemini did not say (or said with a different time, date or day) means the question is asked again.
- **One connection for speech, thinking and voice:** no speech to text, model and voice hand-offs. Gemini is connected and set up before Start, the next weeks of the calendar are kept in memory, and every tool answers Gemini as soon as it is finished.
- **Barge-in:** Gemini stops when you talk over it. Gemini makes sound faster than it is played, so when you talk over the end of an answer that is still playing, the server stops the speakers itself.
- **Safe against retries and races:** a repeated request returns the existing booking, a lost reply is checked before reporting failure, a reschedule updates the same calendar event, and two callers cannot take one slot.
- **Identity checks:** lookup, reschedule and cancel need the booking reference and the phone number, and wrong tries are limited per call and per phone number.
- **Input and output guardrails:** the input guardrail reads Gemini's transcript of the caller. In a turn that tries to override the rules, no tool runs at all, and card numbers are kept off the page and out of the logs. The output guardrail reads Gemini's transcript of its own answer and stops secrets, instruction leaks and "booked it" claims that no tool confirmed. A stopped answer is silenced and Gemini is told to say a fixed sentence instead.
- **Fresh memory per call:** every conversation gets its own Gemini session and an empty short-term memory. Long calls continue on a new connection with Gemini's session resumption when the old one is about to close.
- **Latency table:** each answer is timed from your last word (estimated from the microphone loudness) to the first sound you hear, with the tool time and Gemini's first sound in between.

## Tech Stack

| Area | Technology |
|---|---|
| Language | Python |
| Backend | FastAPI, Uvicorn, WebSockets |
| Speech in, thinking, speech out | Gemini Live API (`gemini-3.8-live`) over a raw WebSocket |
| Calendar | Google Calendar API v3 over httpx |
| Validation | Pydantic |
| Browser audio | AudioWorklet (microphone and player) |
| Testing | pytest, a local fake Gemini Live server and a fake Google Calendar |

## Project Structure

```text
voice-booking-assistant-gemini-live/
  app/
    main.py              FastAPI app, page, health check and the voice WebSocket
    config.py            Reads and validates every setting from .env
    live.py              Gemini Live client: setup, audio, tool answers, reconnect
    session.py           One browser call: Gemini messages, tools, barge-in, guardrails
    prompt.py            Builds the instructions for each conversation
    tools.py             Tool list, input checks and the confirmation gate
    memory.py            Short-term memory for one call
    timing.py            Per-answer latency stopwatch
    booking/             Scheduling rules and the eight tools
    guardrails/          Input and output guardrails
  data/services.json     The services offered
  prompts/               System prompt template
  scripts/               check_calendar.py and check_gemini.py
  static/                Web page, audio worklets, styles
  tests/                 121 tests with a fake Gemini server and a fake calendar
```

## Getting Started

### Prerequisites

- Python 3.14
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/)
- A Google Calendar (a separate test calendar is recommended) and its Calendar ID
- A modern desktop browser and a microphone (headphones recommended)

### Installation

```bash
git clone https://github.com/shahramshafiq/voice-booking-assistant-gemini-live.git
cd voice-booking-assistant-gemini-live
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file with your three private values:

```
GEMINI_API_KEY=
GOOGLE_CALENDAR_API_TOKEN=
GOOGLE_CALENDAR_ID=
```

That is all `.env` needs. Every other setting is read from `.env.example`, where each one is explained in a comment. A value you put in `.env` wins over the one in `.env.example`, so copy a line into `.env` only when you want to change it.

### Environment Variables

The settings you are most likely to change (all names and defaults are in `.env.example`):

| Setting | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY` | (yours) | Gemini API key |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | The Live model |
| `GEMINI_VOICE` | `Kore` | A prebuilt Gemini voice |
| `LIVE_SILENCE_DURATION_MS` | `500` | How long you must be quiet before your turn ends (lower answers sooner, higher cuts you off less) |
| `LIVE_START_SENSITIVITY` / `LIVE_END_SENSITIVITY` | empty | Gemini's speech detection sensitivity (empty = Gemini's default) |
| `LIVE_TRANSCRIPT_WAIT_MS` | `800` | How long a change waits for Gemini's text of your yes |
| `READ_BACK_MIN_SHARE` | `0.6` | Share of the read-back question's words Gemini must say for a yes to count |
| `CONFIRM_HEARD_SHARE` | `0.85` | Share of the question you must have heard if you talk over its end |
| `GOOGLE_CALENDAR_ID` | (yours) | The one calendar the assistant books into |
| `GOOGLE_CALENDAR_API_TOKEN` | (yours) | Manually pasted access token, about one hour long |
| `BUSINESS_NAME` / `BUSINESS_STAFF_NAME` | `TrustCare` / `Shahram Shafiq` | The business and the one staff member all appointments are with |
| `BUSINESS_TIMEZONE` | `Asia/Karachi` | Time zone for every date and time |
| `BUSINESS_HOURS` / `BUSINESS_BREAKS` | `mon-fri 09:00-17:00;sat 10:00-14:00` / `13:00-14:00` | Opening hours and the daily break |
| `CANCELLATION_NOTICE_HOURS` | `24` | Later than this, the caller must contact the staff member |
| `MAX_VERIFY_FAILURES` / `VERIFY_LOCK_SECONDS` | `3` / `600` | Wrong reference or phone tries before a phone number is locked |
| `CONFIRM_TTL_SECONDS` | `180` | How long a confirmation question stays valid |
| `BARGE_IN_ENABLED` | `true` | Whether you can interrupt the assistant (set `false` without headphones) |
| `IDLE_TIMEOUT_SECONDS` | `180` | Stops the call after this long without speech |

### Getting a Google Calendar token

The app uses a manually pasted access token (no login flow). It lasts about one hour.

1. Open the [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground/).
2. In "Input your own scopes" enter `https://www.googleapis.com/auth/calendar.events` and click Authorize APIs.
3. Sign in with the account that owns the calendar and allow access.
4. Click "Exchange authorization code for tokens" and copy the access token.
5. Paste it after `GOOGLE_CALENDAR_API_TOKEN=` in `.env` and restart the server.

### Checking the setup

```bash
python -m scripts.check_calendar
python -m scripts.check_calendar --write
python -m scripts.check_gemini
python -m scripts.check_gemini --speak --tool
```

`check_calendar` checks the token and the calendar (free). `check_gemini` opens the real Gemini setup with the instructions, the eight tools and the voice, which generates nothing. With `--speak` it times one short spoken sentence, and with `--tool` it asks a question that needs a tool and answers the tool with made-up times (the calendar is not used).

### Run

```bash
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, press Start conversation and allow the microphone.

## Usage

Try a full conversation:

1. "I'd like to book a consultation tomorrow afternoon."
2. Pick one of the offered times, then give your name and phone number.
3. The assistant reads back the details and asks to confirm. Say "yes".
4. It books the slot and says a booking reference. Use the reference and the same phone number to look up, move or cancel the booking.

The page shows both transcripts, the tools that were used and a latency table for every answer.

## Testing

```bash
python -m pytest
```

The 121 tests need no keys and cost nothing. A local fake Gemini Live server speaks the same JSON protocol as the real one (binary frames, setup, transcripts, tool calls, interruptions, goAway and session resumption), and a fake Google Calendar answers the calendar requests. The tests cover date parsing, slot maths, the booking rules, the confirmation gate, the guardrails, the Gemini client and whole conversations.

## Limitations and Roadmap

- Gemini hears the audio itself, so the input guardrail cannot hide a request from the model. It stops every tool in that turn instead, and the output guardrail checks what is said.
- Gemini reads the confirmation question out itself, so its transcript is compared with the code's question. A paraphrase that drops too many of its words is asked again.
- The output guardrail works on Gemini's transcript, which usually arrives before the same words are played, but not always.
- The end of your speech in the latency table is estimated from the microphone loudness, because Gemini does not report when it decided you had finished.
- The Google token is pasted by hand and expires after about an hour. One staff member and one calendar. Locks and the race guard work inside one server process.
- The voice is one of Gemini's prebuilt voices. The app runs in the browser only, with no phone line.

## Author

**Shahram Shafiq** - [GitHub](https://github.com/shahramshafiq) | [LinkedIn](https://www.linkedin.com/in/shahramshafiq/)
