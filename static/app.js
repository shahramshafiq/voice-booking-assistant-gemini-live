// The browser side: microphone in, speakers out, and a small interface around them.
//
//   microphone -> mic-worklet.js -> WebSocket -> server
//   server -> WebSocket -> player-worklet.js -> speakers
//
// All numbers (sample rates, chunk size) come from the server's "config" message,
// so they live in .env and not in this file.

const toggleButton = document.getElementById("toggle");
const statusPill = document.getElementById("status");
const interimEl = document.getElementById("interim");
const errorEl = document.getElementById("error");
const warningEl = document.getElementById("warning");
const toolsEl = document.getElementById("tools");
const logEl = document.getElementById("log");
const metricsEl = document.getElementById("metrics");
const averageEl = document.getElementById("average");

const STATUS_LABELS = {
    idle: "Idle",
    connecting: "Connecting...",
    listening: "Listening",
    thinking: "Thinking...",
    speaking: "Speaking",
};

let socket = null;
let audioContext = null;
let micStream = null;
let micSource = null;
let micNode = null;
let micMute = null;
let playerNode = null;
let streaming = false; // true once the server says it is ready for microphone audio
let running = false;
let userBubble = null;
let assistantBubble = null;
const perceivedHistory = [];

// ------------------------------------------------------------------ small UI helpers
function setStatus(state) {
    statusPill.textContent = STATUS_LABELS[state] || state;
    statusPill.className = "pill " + state;
}

function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
}

function clearError() {
    errorEl.hidden = true;
    warningEl.hidden = true;
}

// One line per tool call: the name, whether it worked, and how long it took.
const toolRows = [];
function addToolRow(m) {
    toolRows.push([m.name, m.ok ? "ok" : (m.error || "failed"), m.ms + " ms"]);
    toolsEl.innerHTML = "";
    for (const [name, result, time] of toolRows.slice(-8)) {
        const row = toolsEl.insertRow();
        row.insertCell().textContent = name + " (" + result + ")";
        row.insertCell().textContent = time;
    }
}

function addMessage(role, text) {
    const bubble = document.createElement("div");
    bubble.className = "msg " + role;
    bubble.textContent = text;
    logEl.appendChild(bubble);
    bubble.scrollIntoView({ block: "end" });
    return bubble;
}

function renderMetrics(m) {
    const rows = [
        ["Gemini started answering (first sound, words or tool call)", m.answer_started_ms],
        ["Booking tools started", m.tools_started_ms],
        ["Booking tools finished", m.tools_done_ms],
        ["First sound from Gemini", m.first_audio_ms],
        ["Gemini finished making the whole answer", m.generation_complete_ms],
        ["Sound started playing", m.audio_played_ms],
        ["Silence after the answer before it (for example after a filler)", m.silence_before_ms],
    ];
    metricsEl.innerHTML = "";
    for (const [label, value] of rows) {
        const row = metricsEl.insertRow();
        row.insertCell().textContent = label;
        row.insertCell().textContent = typeof value === "number" ? value + " ms" : (value ?? "n/a");
    }
    if (!m.after_speech) {
        const row = metricsEl.insertRow();
        row.insertCell().textContent = "This answer did not follow your speech, so it has no total.";
        row.insertCell().textContent = "";
    }
    if (typeof m.perceived_ms === "number") {
        const row = metricsEl.insertRow();
        row.className = "total";
        row.insertCell().textContent = "Total: you stopped talking until you heard sound";
        row.insertCell().textContent = m.perceived_ms + " ms";
        // the average is over answers to your words; a result after its filler (it has a silence) is left out
        if (typeof m.silence_before_ms !== "number") perceivedHistory.push(m.perceived_ms);
        const last = perceivedHistory.slice(-10);
        const mean = Math.round(last.reduce((a, b) => a + b, 0) / last.length);
        averageEl.textContent = "Average of the last " + last.length + " replies: " + mean + " ms";
    }
}

// ------------------------------------------------------------------ getting ready before Start is pressed
// The page opens its connection to the server and loads the audio code as soon as it loads. Pressing Start
// then only has to switch the microphone on, and the server has already connected to the speech services.
let serverConfig = null; // the server's first message: sample rates, chunk size...
let socketReady = null; // a promise that resolves with serverConfig once the connection is open
let audioReady = null; // a promise that resolves once the audio code is loaded

function openSocket() {
    if (socketReady) return socketReady;
    socketReady = new Promise((resolve, reject) => {
        const protocol = location.protocol === "https:" ? "wss" : "ws";
        const ws = new WebSocket(protocol + "://" + location.host + "/ws/voice");
        ws.binaryType = "arraybuffer";
        ws.onmessage = (event) => {
            if (typeof event.data === "string") {
                const message = JSON.parse(event.data);
                if (message.type === "config") {
                    serverConfig = message;
                    resolve(message);
                    return;
                }
            }
            onServerMessage(event);
        };
        ws.onclose = () => {
            socket = null;
            socketReady = null;
            serverConfig = null;
            if (running) {
                showError("The connection to the server was lost.");
                stopConversation(false);
            }
        };
        ws.onerror = () => reject(new Error("The connection to the server failed."));
        socket = ws;
    });
    socketReady.catch(() => { socketReady = null; }); // allow a later retry
    return socketReady;
}

function prepareAudio() {
    if (!audioReady) {
        audioReady = (async () => {
            if (!audioContext) audioContext = new AudioContext({ latencyHint: "interactive" });
            await Promise.all([
                audioContext.audioWorklet.addModule("/static/mic-worklet.js"),
                audioContext.audioWorklet.addModule("/static/player-worklet.js"),
            ]);
        })();
        audioReady.catch(() => { audioReady = null; });
    }
    return audioReady;
}

window.addEventListener("load", () => {
    prepareAudio().catch(() => {});
    openSocket().catch(() => {});
});

// ------------------------------------------------------------------ start and stop
async function startConversation() {
    clearError();
    running = true;
    toggleButton.disabled = true;
    setStatus("connecting");
    try {
        if (!audioContext) audioContext = new AudioContext({ latencyHint: "interactive" });
        const resumed = audioContext.resume(); // must be called straight from the click
        await Promise.all([resumed, prepareAudio(), openSocket()]);
        await buildAudioGraph(await openSocket());
        socket.send(JSON.stringify({ type: "start" }));
    } catch (error) {
        showError("Could not start: " + error.message);
        stopConversation(false);
    }
}

function stopConversation(notifyServer = true) {
    running = false;
    streaming = false;
    if (notifyServer && socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "stop" }));
    }
    // the connection to the server stays open, so the next Start is instant
    if (micStream) micStream.getTracks().forEach((track) => track.stop());
    for (const node of [micSource, micNode, micMute, playerNode]) {
        if (node) node.disconnect();
    }
    micStream = micSource = micNode = micMute = playerNode = null;
    interimEl.textContent = "";
    toggleButton.textContent = "Start conversation";
    toggleButton.classList.remove("stop");
    toggleButton.disabled = false;
    setStatus("idle");
}

toggleButton.addEventListener("click", () => {
    if (running) stopConversation(true);
    else startConversation();
});

// ------------------------------------------------------------------ building the audio graph
async function buildAudioGraph(config) {
    // speakers
    playerNode = new AudioWorkletNode(audioContext, "player-processor", {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: { inputRate: config.output_sample_rate },
    });
    playerNode.connect(audioContext.destination);
    playerNode.port.onmessage = (event) => {
        const message = event.data;
        if (!socket || socket.readyState !== WebSocket.OPEN) return;
        if (message.type === "started") socket.send(JSON.stringify({ type: "playback_started" }));
        if (message.type === "progress") socket.send(JSON.stringify({ type: "playback_progress", played_ms: Math.round(message.playedMs) }));
        if (message.type === "drained") socket.send(JSON.stringify({ type: "playback_drained" }));
    };

    // microphone (echo cancellation matters: it stops the assistant hearing itself)
    micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    micSource = audioContext.createMediaStreamSource(micStream);
    micNode = new AudioWorkletNode(audioContext, "mic-processor", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: { targetRate: config.mic_sample_rate, chunkMs: config.mic_chunk_ms },
    });
    micMute = audioContext.createGain(); // the mic node must be connected to the output to run; mute it
    micMute.gain.value = 0;
    micSource.connect(micNode);
    micNode.connect(micMute);
    micMute.connect(audioContext.destination);
    micNode.port.onmessage = (event) => {
        if (streaming && socket && socket.readyState === WebSocket.OPEN) socket.send(event.data);
    };
}

// ------------------------------------------------------------------ messages from the server
function onServerMessage(event) {
    // binary = reply audio: hand it straight to the player
    if (typeof event.data !== "string") {
        if (playerNode) playerNode.port.postMessage({ type: "audio", pcm: event.data }, [event.data]);
        return;
    }

    const message = JSON.parse(event.data);
    switch (message.type) {
        case "ready":
            streaming = true;
            toggleButton.textContent = "Stop conversation";
            toggleButton.classList.add("stop");
            toggleButton.disabled = false;
            break;
        case "state":
            setStatus(message.value);
            // the server went back to idle by itself (a failed start, a lost connection): reset the buttons too
            if (message.value === "idle" && running) stopConversation(false);
            break;
        case "turn":
            // a new turn: the caller's words and the answer get their bubbles now, so they stay in order
            // even when the answer starts before Gemini's text of the caller's words arrives
            userBubble = addMessage("user", "");
            assistantBubble = addMessage("assistant", "");
            break;
        case "user_text":
            if (userBubble) userBubble.textContent = message.text;
            break;
        case "reply":
            // a second answer in the same turn (for example one that replaces a stopped answer) gets its own bubble
            if (!assistantBubble || assistantBubble.textContent) assistantBubble = addMessage("assistant", "");
            break;
        case "tool":
            addToolRow(message);
            break;
        case "warning":
            warningEl.textContent = message.message;
            warningEl.hidden = false;
            break;
        case "calendar_ok":
            warningEl.hidden = true;
            break;
        case "assistant_text":
            if (assistantBubble) assistantBubble.textContent = message.text;
            break;
        case "audio_end":
            if (playerNode) playerNode.port.postMessage({ type: "end" });
            break;
        case "clear":
            if (playerNode) playerNode.port.postMessage({ type: "clear" });
            break;
        case "interrupted":
            if (assistantBubble) assistantBubble.classList.add("interrupted");
            break;
        case "metrics":
            renderMetrics(message);
            break;
        case "idle_timeout":
            showError("Stopped listening because nobody spoke for a while. Press Start to continue.");
            break;
        case "error":
            showError(message.message);
            break;
    }
}

window.addEventListener("beforeunload", () => {
    stopConversation(true);
    if (socket) socket.close();
});