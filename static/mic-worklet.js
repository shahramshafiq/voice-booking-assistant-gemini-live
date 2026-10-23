// Runs on the browser's audio thread. Turns microphone audio into small 16-bit PCM chunks.
//
// The microphone usually runs at 44100 or 48000 samples per second. Gemini Live wants 16000.
// We average the input samples that fall into each output sample (a simple low-pass filter)
// and send a chunk every `chunkMs` milliseconds.
class MicProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const settings = options.processorOptions;
        this.ratio = sampleRate / settings.targetRate; // e.g. 48000 / 16000 = 3
        this.chunkSamples = Math.round((settings.targetRate * settings.chunkMs) / 1000);
        this.chunk = new Int16Array(this.chunkSamples);
        this.chunkIndex = 0;
        this.sum = 0;
        this.count = 0;
        this.phase = 0;
    }

    process(inputs) {
        const input = inputs[0] && inputs[0][0];
        if (!input) return true;

        for (let i = 0; i < input.length; i++) {
            this.sum += input[i];
            this.count += 1;
            this.phase += 1;

            if (this.phase >= this.ratio) {
                this.phase -= this.ratio;
                const average = this.sum / this.count;
                const clamped = Math.max(-1, Math.min(1, average));
                this.chunk[this.chunkIndex++] = clamped < 0 ? clamped * 32768 : clamped * 32767;
                this.sum = 0;
                this.count = 0;

                if (this.chunkIndex === this.chunkSamples) {
                    const buffer = this.chunk.buffer;
                    this.port.postMessage(buffer, [buffer]);
                    this.chunk = new Int16Array(this.chunkSamples);
                    this.chunkIndex = 0;
                }
            }
        }
        return true;
    }
}

registerProcessor("mic-processor", MicProcessor);