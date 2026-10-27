"""Tests for loading the recorded filler sentences."""
import json
from dataclasses import replace

from app.config import settings
from app.fillers import load_clips


def test_recordings_are_loaded_for_the_configured_voice(tmp_path):
    voice = tmp_path / "Kore"
    voice.mkdir()
    (voice / "book_1.pcm").write_bytes(b"\x01\x00" * 10)
    (voice / "index.json").write_text(json.dumps([{"kind": "book", "text": "One moment.", "file": "book_1.pcm"}]))
    clips = load_clips(replace(settings, filler_clips_dir=str(tmp_path), gemini_voice="Kore"))
    assert clips == {"book": [("One moment.", b"\x01\x00" * 10)]}


def test_no_recordings_or_a_broken_index_means_none_are_played(tmp_path):
    assert load_clips(replace(settings, filler_clips_dir=str(tmp_path), gemini_voice="Puck")) == {}
    voice = tmp_path / "Puck"
    voice.mkdir()
    (voice / "index.json").write_text("not json")
    assert load_clips(replace(settings, filler_clips_dir=str(tmp_path), gemini_voice="Puck")) == {}
