"""A stopwatch for one answer.

Every event is stored with the time it first happened. The summary reports each event in milliseconds
relative to "speech_end": the moment the caller stopped talking, estimated from the microphone loudness
(Gemini does its own turn detection and does not say when it decided the caller had finished).
"""
import time


class TurnTimer:
    def __init__(self):
        self.marks = {}

    def mark(self, name, at=None):
        """Record an event, now or at the perf_counter time `at`. Only the FIRST time counts."""
        if name not in self.marks:
            self.marks[name] = time.perf_counter() if at is None else at

    def has(self, name):
        return name in self.marks

    def summary(self):
        start = self.marks.get("speech_end")
        if start is None:
            return {}
        return {
            f"{name}_ms": round((at - start) * 1000)
            for name, at in sorted(self.marks.items(), key=lambda item: item[1])
            if name != "speech_end"
        }
