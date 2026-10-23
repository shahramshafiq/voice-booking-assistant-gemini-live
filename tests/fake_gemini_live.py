"""A pretend Gemini Live server for the tests. It speaks the same JSON protocol as the real one.

The test plays the part of Gemini: it decides what the "caller" said (an input transcript) and what the
"model" does next (speak, call a tool, finish the turn, get interrupted). Nothing here uses the internet or a key.
Like the real service it answers in binary frames and closes with a code and a reason when something is wrong.
"""
import asyncio
import base64
import json
from urllib.parse import parse_qs, urlparse

import websockets

OUTPUT_RATE = 24000  # Gemini answers with 16-bit PCM at 24 kHz


class FakeGeminiLive:
    def __init__(self):
        self.url = None
        self.setups = []  # the setup message of every connection, in order
        self.keys = []  # the key each connection used
        self.handles = []  # the resumption handle each setup carried (None for a new session)
        self.audio = bytearray()  # all microphone audio received
        self.audio_mime = set()
        self.tool_responses = asyncio.Queue()
        self.notes = []  # text notes (clientContent) received
        self.connection = None  # the newest connection
        self.connections = 0
        self.closed_connections = 0
        self.reject = None  # (code, reason): close every new connection after its setup, like a wrong key
        self.setup_delay = 0.0  # seconds before the setup is answered (a slow connection)
        self._server = None

    async def start(self):
        self._server = await websockets.serve(self._serve, "127.0.0.1", 0)
        port = list(self._server.sockets)[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/ws/live"
        return self

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, connection):
        query = parse_qs(urlparse(connection.request.path).query)
        self.keys.append(query.get("key", [""])[0])
        try:
            setup = json.loads(await connection.recv())
        except websockets.ConnectionClosed:
            return
        self.setups.append(setup)
        self.handles.append(setup.get("setup", {}).get("sessionResumption", {}).get("handle"))
        await asyncio.sleep(self.setup_delay)
        if self.reject:
            await connection.close(*self.reject)
            return
        if "setup" not in setup:
            await connection.close(1007, "The first message must be setup.")
            return
        self.connections += 1
        self.connection = connection
        try:
            await connection.send(json.dumps({"setupComplete": {}}).encode())
            async for raw in connection:
                message = json.loads(raw)
                if "realtimeInput" in message:
                    audio = message["realtimeInput"].get("audio")
                    if audio:
                        self.audio += base64.b64decode(audio["data"])
                        self.audio_mime.add(audio["mimeType"])
                elif "toolResponse" in message:
                    await self.tool_responses.put(message["toolResponse"]["functionResponses"])
                elif "clientContent" in message:
                    self.notes.append(message["clientContent"]["turns"][0]["parts"][0]["text"])
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed_connections += 1

    # ================================================================ the test plays Gemini
    async def send(self, message):
        await self.connection.send(json.dumps(message).encode())

    async def hear(self, text):
        """Gemini's transcript of what the caller said (it comes in pieces, like the real one)."""
        await self.send({"serverContent": {"inputTranscription": {"text": text}}})

    async def say(self, text, audio_ms=500):
        """Gemini speaks: its transcript of the sentence, then the sound in two pieces."""
        await self.send({"serverContent": {"outputTranscription": {"text": text}}})
        pcm = b"\x01\x00" * (OUTPUT_RATE * audio_ms // 1000)
        half = len(pcm) // 4 * 2
        for piece in (pcm[:half], pcm[half:]):
            await self.send({"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"mimeType": f"audio/pcm;rate={OUTPUT_RATE}", "data": base64.b64encode(piece).decode()}}]}}})

    async def finish(self, total_tokens=None):
        await self.send({"serverContent": {"generationComplete": True}})
        message = {"serverContent": {"turnComplete": True}}
        if total_tokens is not None:
            message["usageMetadata"] = {"promptTokenCount": total_tokens - 50, "responseTokenCount": 50, "totalTokenCount": total_tokens}
        await self.send(message)

    async def interrupt(self):
        await self.send({"serverContent": {"interrupted": True}})

    async def call_tool(self, name, args, call_id="call-1", wait=True):
        """Gemini asks for a tool. With wait=True, returns the response the session sent back."""
        await self.send({"toolCall": {"functionCalls": [{"id": call_id, "name": name, "args": args}]}})
        if wait:
            return (await asyncio.wait_for(self.tool_responses.get(), 5))[0]

    async def cancel_tool(self, call_id):
        await self.send({"toolCallCancellation": {"ids": [call_id]}})

    async def go_away(self, handle):
        """Gemini gives a resumption handle, then says this connection will close soon."""
        await self.send({"sessionResumptionUpdate": {"newHandle": handle, "resumable": True}})
        await self.send({"goAway": {"timeLeft": "5s"}})
