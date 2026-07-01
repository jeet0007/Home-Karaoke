// AudioWorkletProcessor that buffers mono mic samples off the main thread
// and flushes them to it in fixed-size chunks, for streaming to /grade.
// See templates/player.html for how this is wired up (and its
// ScriptProcessorNode fallback for browsers without AudioWorklet support).
class GradingProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunkSize = 2048;
    this._buffer = new Float32Array(this._chunkSize);
    this._writeIndex = 0;
  }

  process(inputs) {
    const channelData = inputs[0] && inputs[0][0];
    if (channelData) {
      for (let i = 0; i < channelData.length; i++) {
        this._buffer[this._writeIndex++] = channelData[i];
        if (this._writeIndex >= this._chunkSize) {
          this.port.postMessage(this._buffer.slice(0));
          this._writeIndex = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('grading-processor', GradingProcessor);
