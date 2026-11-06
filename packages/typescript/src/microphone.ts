export type Resampling = { tail: Float32Array; position: number; phases?: Float64Array[] }

export const MICROPHONE: MediaTrackConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
}

const PROCESSOR = 'senses-capture'

export function downsample(
  state: Resampling,
  input: Float32Array,
  inRate: number,
  outRate: number
): Float32Array {
  const crossings = 16
  const passband = 0.9
  const divisor = (a: number, b: number): number => (b === 0 ? a : divisor(b, a % b))
  const shared = divisor(inRate, outRate)
  const steps = outRate / shared
  const advance = inRate / shared
  const ratio = inRate / outRate
  const cutoff = ratio > 1 ? passband / ratio : 1
  const reach = Math.ceil(crossings * Math.max(1, ratio))
  state.phases ??= Array.from({ length: steps }, (_, phase) =>
    Float64Array.from({ length: 2 * reach }, (_, tap) => {
      const offset = tap - reach + 1 - phase / steps
      const turn = (Math.PI * offset) / reach
      const taper = 0.42 + 0.5 * Math.cos(turn) + 0.08 * Math.cos(2 * turn)
      const sinc = offset === 0 ? cutoff : Math.sin(Math.PI * cutoff * offset) / (Math.PI * offset)
      return taper * sinc
    })
  )
  const buffer = new Float32Array(state.tail.length + input.length)
  buffer.set(state.tail, 0)
  buffer.set(input, state.tail.length)
  const out: number[] = []
  let position = state.position
  while (Math.floor(position / steps) + reach < buffer.length) {
    const first = Math.floor(position / steps) - reach + 1
    const taps = state.phases[position % steps] as Float64Array
    let sample = 0
    for (let tap = 0; tap < taps.length; tap++)
      sample += (buffer[first + tap] ?? 0) * (taps[tap] as number)
    out.push(sample)
    position += advance
  }
  const consumed = Math.max(0, Math.floor(position / steps) - reach + 1)
  state.tail = buffer.slice(consumed)
  state.position = position - consumed * steps
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
