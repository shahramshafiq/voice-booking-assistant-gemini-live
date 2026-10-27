"""The FastAPI application: the web page, a health check and the voice WebSocket."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .booking.calendar_client import CalendarClient
from .booking.service import BookingService
from .config import settings
from .fillers import load_clips
from .guardrails.input_rail import InputRail
from .guardrails.output_rail import OutputRail
from .logging_setup import setup_logging
from .prompt import build_instructions
from .session import VoiceSession

setup_logging(settings.log_level)
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app):
    # everything below is created once and shared by every browser session
    calendar = CalendarClient(settings)
    service = BookingService(settings, calendar)
    app.state.calendar = calendar
    app.state.service = service
    app.state.input_rail = InputRail(settings)
    # the output guardrail protects the rules part of the instructions, which is the same for every conversation
    app.state.output_rail = OutputRail(settings, build_instructions(settings, service.now()))
    app.state.filler_clips = load_clips(settings)

    log.info("ready: Gemini Live model=%s voice=%s calendar=%s", settings.gemini_live_model, settings.gemini_voice,
             settings.google_calendar_id)
    yield
    await calendar.close()
    log.info("shut down")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws/voice")
async def voice_socket(websocket: WebSocket):
    await websocket.accept()
    log.info("browser connected")
    state = websocket.app.state
    session = VoiceSession(websocket, settings, state.service, state.input_rail, state.output_rail, state.filler_clips)
    await session.run()
    log.info("browser disconnected")
