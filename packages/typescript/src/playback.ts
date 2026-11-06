import type { AudioOut } from './wire.js'

export type AudioContextLike = Pick<
  AudioContext,
  'currentTime' | 'destination' | 'createBuffer' | 'createBufferSource' | 'createGain' | 'close'
> & { readonly outputLatency?: number }

const FADE_S = 0.06
const START_S = 0.12

type Chunk = { start: number; duration: number }
type Scheduled = { utterance: number; start: number; node: AudioBufferSourceNode }

export class Playback {
  private next = 0
  private fenced = 0
  private gain: GainNode | null = null
  private readonly scheduled = new Set<Scheduled>()
  private readonly played = new Map<number, Chunk[]>()
  private readonly ending = new Set<number>()
  private readonly reported = new Set<number>()

  constructor(
    private readonly context: AudioContextLike,
    private readonly report: (utterance: number, ms: number) => void
  ) {}

  speaking(utterance: number): void {
    if (utterance > this.fenced && !this.played.has(utterance) && !this.reported.has(utterance))
      this.played.set(utterance, [])
  }

  push(frame: AudioOut): void {
    if (frame.utterance <= this.fenced) return
    this.speaking(frame.utterance)
    const samples = frame.pcm.byteLength / 2 / frame.channels
    if (samples === 0) return
    const buffer = this.context.createBuffer(frame.channels, samples, frame.sampleRate)
    const view = new DataView(frame.pcm.buffer, frame.pcm.byteOffset, frame.pcm.byteLength)
    for (let channel = 0; channel < frame.channels; channel++) {
      const data = buffer.getChannelData(channel)
      for (let i = 0; i < samples; i++) {
        const value = view.getInt16((i * frame.channels + channel) * 2, true)
        data[i] = value < 0 ? value / 0x8000 : value / 0x7fff
      }
    }
    const node = this.context.createBufferSource()
    node.buffer = buffer
    node.connect(this.output())
    const now = this.context.currentTime
    const start = this.next > now ? this.next : now + START_S
    const entry: Scheduled = { utterance: frame.utterance, start, node }
    this.played.get(frame.utterance)?.push({ start, duration: buffer.duration })
    node.onended = () => {
      this.scheduled.delete(entry)
      this.settle(entry.utterance)
    }
    node.start(start)
    this.next = start + buffer.duration
    this.scheduled.add(entry)
  }

  silent(utterance: number): void {
    this.ending.add(utterance)
    this.settle(utterance)
  }

  stop(utterance: number): void {
    if (utterance !== Number.MAX_SAFE_INTEGER) this.speaking(utterance)
    this.fenced = Math.max(this.fenced, utterance)
    const now = this.context.currentTime
    this.gain?.gain.setValueAtTime(1, now)
    this.gain?.gain.linearRampToValueAtTime(0, now + FADE_S)
    this.gain = null
    for (const entry of this.scheduled) {
      entry.node.onended = null
      entry.node.stop(Math.max(now + FADE_S, entry.start))
    }
    this.scheduled.clear()
    this.next = now
    const reached = now - (this.context.outputLatency ?? 0)
    for (const chunks of this.played.values())
      for (const chunk of chunks)
        chunk.duration = Math.min(Math.max(reached - chunk.start, 0), chunk.duration)
    for (const number of [...this.played.keys()].sort((a, b) => a - b))
      if (number <= utterance) this.finish(number)
  }

  async close(): Promise<void> {
    this.stop(Number.MAX_SAFE_INTEGER)
    await this.context.close()
  }

  private output(): GainNode {
    if (this.gain === null) {
      this.gain = this.context.createGain()
      this.gain.connect(this.context.destination)
    }
    return this.gain
  }

  private settle(utterance: number): void {
    if (!this.ending.has(utterance)) return
    for (const entry of this.scheduled) if (entry.utterance === utterance) return
    this.finish(utterance)
  }

  private finish(utterance: number): void {
    const chunks = this.played.get(utterance)
    if (chunks === undefined) return
    this.played.delete(utterance)
    this.ending.delete(utterance)
    this.reported.add(utterance)
    const seconds = chunks.reduce((sum, chunk) => sum + chunk.duration, 0)
    this.report(utterance, Math.round(seconds * 1000))
  }
}
