// Runs on the browser's audio thread. Plays the reply audio the server sends.
//
// Incoming 16-bit PCM is stored in a ring buffer. Every 128 output samples the browser asks
// process() for more sound; we read from the buffer and convert from the server's sample
// rate (24000 from Gemini Live) to the speaker's rate (e.g. 48000) by blending neighbouring samples.
//
// Messages IN  (from app.js):  {type: "audio", pcm}   {type: "end"}   {type: "clear"}
// Messages OUT (to app.js):    {type: "started"}  {type: "progress", playedMs}  {type: "drained"}
class PlayerProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this.inputRate = options.processorOptions.inputRate;
        this.capacity = this.inputRate * 120; // room for 120 seconds of audio
        this.ring = new Float32Array(this.capacity);
        this.resetState();

        this.port.onmessage = (event) => {
            const message = event.data;
            if (message.type === "audio") {
                const pcm = new Int16Array(message.pcm);
                for (let i = 0; i < pcm.length; i++) {
                    this.ring[this.written % this.capacity] = pcm[i] / 32768;
                    this.written += 1;
                }
            } else if (message.type === "end") {
                this.ended = true; // the server will send no more audio for this reply
            } else if (message.type === "clear") {
                this.resetState(); // barge-in: throw everything away
            }
        };
    }

    resetState() {
        this.written = 0; // total samples received
        this.readPos = 0; // position of the next sample to play (can be fractional)
        this.ended = false;
        this.started = false;
        this.sinceReport = 0;
    }

    process(inputs, outputs) {
        const output = outputs[0][0];
        const step = this.inputRate / sampleRate;

        for (let i = 0; i < output.length; i++) {
            const base = Math.floor(this.readPos);
            if (base + 1 >= this.written) {
                output[i] = 0; // nothing to play: silence
                continue;
            }
            if (!this.started) {
                this.started = true;
                this.port.postMessage({ type: "started" });
            }
            const fraction = this.readPos - base;
            const a = this.ring[base % this.capacity];
            const b = this.ring[(base + 1) % this.capacity];
            output[i] = a + (b - a) * fraction;
            this.readPos += step;
        }

        if (this.started) {
            this.sinceReport += output.length;
            if (this.sinceReport >= sampleRate / 10) {
                this.sinceReport = 0;
                this.port.postMessage({ type: "progress", playedMs: (this.readPos / this.inputRate) * 1000 });
            }
        }

        if (this.ended && Math.floor(this.readPos) + 1 >= this.written) {
            this.port.postMessage({ type: "progress", playedMs: (this.readPos / this.inputRate) * 1000 });
            this.port.postMessage({ type: "drained" });
            this.resetState();
        }
        return true;
    }
}

registerProcessor("player-processor", PlayerProcessor);