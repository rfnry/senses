import { SensesException } from './errors.js'
import type { SensesAudio } from './room.js'

type Spoken = { start: number; end: number | null; hole: number | null; stalled: number | null }

const POLL_MS = 100
const FADE_MS = 60
const FADE_STEPS = 6
const PACKET_MS = 20
const STALL_POLLS = 6
const STILL_POLLS = 10
const HOLE_PACKETS = 15
const TAIL_MS = 100

const counted = (packets: unknown) => (typeof packets === 'number' ? Math.max(0, packets) : 0)

const outputMs = (playout: Record<string, unknown> | undefined) => {
  const delay = playout?.totalPlayoutDelay
  const samples = playout?.totalSamplesCount
  return typeof delay === 'number' && typeof samples === 'number' && samples > 0
    ? (delay / samples) * 1000
    : 0
}

export class RemotePlayback {
  private element: HTMLAudioElement | null = null
  private receiver: RTCRtpReceiver | null = null
  private fenced = 0
  private fading: ReturnType<typeof setInterval> | null = null
  private watching: Promise<void> | null = null
  private lost = 0
  private holes = 0
  private shift = 0
  private rest: number | null = null
  private idle = 0
  private last: number | null = null
  private output = 0
  private readonly spoken = new Map<number, Spoken>()
  private readonly reported = new Set<number>()

  constructor(
    private readonly report: (utterance: number, ms: number) => void,
    private readonly hearing: (audio: SensesAudio) => void = () => {}
  ) {}

  start(stream: MediaStream, receiver: RTCRtpReceiver): void {
    this.receiver = receiver
    const element = new Audio()
    element.autoplay = true
    element.srcObject = stream
    this.element = element
    void Promise.resolve(element.play()).catch(() => this.hearing('blocked'))
  }

  async resume(): Promise<void> {
    try {
      await this.element?.play()
    } catch {
      throw new SensesException('senses: the browser would not play the voice')
    }
    this.hearing('playing')
  }

  speaking(utterance: number, sentMs: number): void {
    if (this.rest !== null) {
      this.shift = sentMs - this.rest
      this.last = null
    }
    this.rest = null
    this.idle = 0
    if (utterance <= this.fenced || this.spoken.has(utterance) || this.reported.has(utterance))
      return
    this.stopFading()
    if (this.element !== null) {
      this.element.volume = 1
      this.element.muted = false
    }
    for (const earlier of this.spoken.values()) earlier.end ??= sentMs
    this.spoken.set(utterance, { start: sentMs, end: null, hole: null, stalled: null })
    void this.watch()
  }

  async stop(utterance: number, sentMs: number): Promise<void> {
    this.fenced = Math.max(this.fenced, utterance)
    this.rest = null
    void this.watch()
    this.fadeOut()
    if (!this.spoken.has(utterance) && !this.reported.has(utterance)) {
      this.reported.add(utterance)
      this.report(utterance, 0)
    }
    const ending = [...this.spoken.keys()]
      .filter((number) => number <= utterance)
      .sort((a, b) => a - b)
    if (ending.length === 0) return
    const now = await this.position()
    const behind = now !== null && sentMs - now >= STILL_POLLS * POLL_MS
    for (const number of ending)
      this.finish(number, now === null ? null : now - this.output, behind)
  }

  silent(utterance: number, sentMs: number): void {
    const spoken = this.spoken.get(utterance)
    if (spoken === undefined) return
    spoken.end ??= sentMs
    void this.watch()
  }

  close(): void {
    this.stopFading()
    this.element?.pause()
    if (this.element !== null) this.element.srcObject = null
    this.element = null
    this.receiver = null
  }

  private fadeOut(): void {
    const element = this.element
    if (element === null || element.muted || this.fading !== null) return
    let step = 0
    this.fading = setInterval(() => {
      step += 1
      element.volume = Math.max(0, 1 - step / FADE_STEPS)
      if (step < FADE_STEPS) return
      element.muted = true
      this.stopFading()
    }, FADE_MS / FADE_STEPS)
  }

  private stopFading(): void {
    if (this.fading !== null) clearInterval(this.fading)
    this.fading = null
  }

  private watch(): Promise<void> {
    this.watching ??= this.poll().finally(() => {
      this.watching = null
    })
    return this.watching
  }

  private async poll(): Promise<void> {
    this.last = null
    this.idle = 0
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS))
      const now = await this.position()
      if (now !== this.last && this.idle >= STILL_POLLS) {
        const spoken = this.holding(this.last)
        if (spoken !== undefined) spoken.stalled ??= this.last
      }
      this.idle = now === this.last ? this.idle + 1 : 0
      this.last = now
      for (const [utterance, { end }] of this.spoken) {
        if (end === null) break
        if (now === null) this.finish(utterance, null)
        else if (now >= end || (this.idle > 0 && end - now <= TAIL_MS)) this.finish(utterance, end)
        else if (this.idle >= STALL_POLLS) this.finish(utterance, now, true)
        else break
      }
      if (now === null) return
      if (this.idle >= STILL_POLLS && this.spoken.size === 0) {
        this.rest = now - this.shift
        return
      }
    }
  }

  private holding(ms: number | null): Spoken | undefined {
    if (ms === null) return undefined
    return [...this.spoken.values()].find(
      ({ start, end }) => ms > start && (end === null || ms < end)
    )
  }

  private finish(utterance: number, at: number | null, short = false): void {
    const spoken = this.spoken.get(utterance)
    if (spoken === undefined) return
    this.spoken.delete(utterance)
    this.reported.add(utterance)
    if (at === null) return
    const { start, end, hole, stalled } = spoken
    const stops = [at, end, hole, short ? stalled : null]
    const reached = Math.min(...stops.filter((ms) => ms !== null))
    this.report(utterance, Math.max(0, Math.round(reached - start)))
  }

  private async position(): Promise<number | null> {
    const stats = await this.receiver?.getStats().catch(() => null)
    if (stats === null || stats === undefined) return null
    if (this.element?.paused === true) return this.shift
    const entries = Array.from(stats.values()) as Array<Record<string, unknown>>
    const inbound = entries.find((entry) => entry.type === 'inbound-rtp')
    if (inbound === undefined) return this.shift
    const { jitterBufferEmittedCount: emitted, codecId, playoutId } = inbound
    if (typeof emitted !== 'number') return null
    const codec = entries.find((entry) => entry.id === codecId)
    const rate = typeof codec?.clockRate === 'number' ? codec.clockRate : 48000
    const lost = counted(inbound.packetsLost) + counted(inbound.packetsDiscarded)
    if (lost - this.lost > HOLE_PACKETS) {
      this.holes += lost - this.lost
      const spoken = this.holding(this.last)
      if (spoken !== undefined) spoken.hole ??= this.last
    }
    this.lost = Math.max(this.lost, lost)
    this.output = outputMs(entries.find((entry) => entry.id === playoutId))
    return (emitted * 1000) / rate + (lost - this.holes) * PACKET_MS + this.shift
  }
}
