export type Resampling = { tail: Float32Array; position: number }

export const MICROPHONE: MediaTrackConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
}

const PROCESSOR = 'rfnry-senses-capture'

export function downsample(state: Resampling, input: Float32Array, inRate: number, outRate: number): Float32Array {
  const buffer = new Float32Array(state.tail.length + input.length)
  buffer.set(state.tail, 0)
  buffer.set(input, state.tail.length)
  const step = inRate / outRate
  const out: number[] = []
  let position = state.position
  while (Math.floor(position) + 1 < buffer.length) {
    const low = Math.floor(position)
    const t = position - low
    out.push((buffer[low] ?? 0) * (1 - t) + (buffer[low + 1] ?? 0) * t)
    position += step
  }
  const consumed = Math.floor(position)
  state.tail = buffer.slice(consumed)
  state.position = position - consumed
  return Float32Array.from(out)
}

export function toPcm16(samples: Float32Array): Uint8Array {
  const out = new Uint8Array(samples.length * 2)
  const view = new DataView(out.buffer)
  for (let i = 0; i < samples.length; i++) {
    const sample = Math.max(-1, Math.min(1, samples[i] ?? 0))
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true)
  }
  return out
}

const WORKLET = `
const downsample = ${String(downsample)}
const toPcm16 = ${String(toPcm16)}
class Capture extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.rate = options.processorOptions.rate
    this.frame = options.processorOptions.frame
    this.state = { tail: new Float32Array(0), position: 0 }
    this.held = new Float32Array(0)
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (!channel) return true
    const out = downsample(this.state, channel, sampleRate, this.rate)
    const joined = new Float32Array(this.held.length + out.length)
    joined.set(this.held, 0)
    joined.set(out, this.held.length)
    let offset = 0
    while (joined.length - offset >= this.frame) {
      const pcm = toPcm16(joined.subarray(offset, offset + this.frame))
      this.port.postMessage(pcm.buffer, [pcm.buffer])
      offset += this.frame
    }
    this.held = joined.slice(offset)
    return true
  }
}
registerProcessor('${PROCESSOR}', Capture)
`

export class Microphone {
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private node: AudioWorkletNode | null = null
  private on = true
  private readonly listeners = new Set<(pcm: Uint8Array) => void>()

  async start(sampleRate: number): Promise<MediaStream> {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: MICROPHONE })
    this.stream = stream
    const context = new AudioContext()
    this.context = context
    const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }))
    try {
      await context.audioWorklet.addModule(url)
    } finally {
      URL.revokeObjectURL(url)
    }
    const node = new AudioWorkletNode(context, PROCESSOR, {
      processorOptions: { rate: sampleRate, frame: Math.round(sampleRate / 50) },
    })
    node.port.onmessage = (message: MessageEvent<ArrayBuffer>) => {
      if (!this.on) return
      const pcm = new Uint8Array(message.data)
      for (const listener of this.listeners) listener(pcm)
    }
    context.createMediaStreamSource(stream).connect(node)
    this.node = node
    return stream
  }

  onChunk(listener: (pcm: Uint8Array) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  get enabled(): boolean {
    return this.on
  }

  set enabled(on: boolean) {
    this.on = on
    for (const track of this.stream?.getAudioTracks() ?? []) track.enabled = on
  }

  async stop(): Promise<void> {
    this.node?.disconnect()
    this.node = null
    for (const track of this.stream?.getTracks() ?? []) track.stop()
    this.stream = null
    await this.context?.close()
    this.context = null
  }
}
