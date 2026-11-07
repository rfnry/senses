type Spoken = { start: number; end: number | null }

const POLL_MS = 100
const FADE_MS = 60
const FADE_STEPS = 6
const PACKET_MS = 20
const STALL_POLLS = 6
const REST_POLLS = 10
const CONCEALED_PACKETS = 5
const CONCEALED_MS = CONCEALED_PACKETS * PACKET_MS

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
  private output = 0
  private readonly spoken = new Map<number, Spoken>()
  private readonly reported = new Set<number>()

  constructor(private readonly report: (utterance: number, ms: number) => void) {}

  start(stream: MediaStream, receiver: RTCRtpReceiver): void {
    this.receiver = receiver
    const element = new Audio()
    element.autoplay = true
    element.srcObject = stream
    void Promise.resolve(element.play()).catch(() => {})
    this.element = element
  }

  speaking(utterance: number, sentMs: number): void {
    if (this.rest !== null) this.shift = sentMs - this.rest
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
    this.spoken.set(utterance, { start: sentMs, end: null })
    void this.watch()
  }

  async stop(utterance: number): Promise<void> {
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
    for (const number of ending) this.finish(number, now === null ? null : now - this.output)
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
    let last: number | null = null
    this.idle = 0
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS))
      const now = await this.position()
      this.idle = now === last ? this.idle + 1 : 0
      last = now
      for (const [utterance, { end }] of this.spoken) {
        if (end === null) break
        if (now === null) this.finish(utterance, null)
        else if (now >= end || (this.idle > 0 && end - now <= CONCEALED_MS))
          this.finish(utterance, end)
        else if (this.idle >= STALL_POLLS) this.finish(utterance, now)
        else break
      }
      if (now === null) return
      if (this.idle >= REST_POLLS && this.spoken.size === 0) {
        this.rest = now - this.shift
        return
      }
    }
  }

  private finish(utterance: number, at: number | null): void {
    const spoken = this.spoken.get(utterance)
    if (spoken === undefined) return
    this.spoken.delete(utterance)
    this.reported.add(utterance)
    const { start, end } = spoken
    const reached = at === null ? start : Math.min(at, end ?? at)
    this.report(utterance, Math.max(0, Math.round(reached - start)))
  }

  private async position(): Promise<number | null> {
    const stats = await this.receiver?.getStats().catch(() => null)
    if (stats === null || stats === undefined) return null
    if (this.element?.paused === true) return 0
    const entries = Array.from(stats.values()) as Array<Record<string, unknown>>
    const inbound = entries.find((entry) => entry.type === 'inbound-rtp')
    if (inbound === undefined) return this.shift
    const { jitterBufferEmittedCount: emitted, codecId, playoutId } = inbound
    if (typeof emitted !== 'number') return null
    const codec = entries.find((entry) => entry.id === codecId)
    const rate = typeof codec?.clockRate === 'number' ? codec.clockRate : 48000
    const lost = counted(inbound.packetsLost) + counted(inbound.packetsDiscarded)
    if (lost - this.lost > CONCEALED_PACKETS) this.holes += lost - this.lost
    this.lost = Math.max(this.lost, lost)
    this.output = outputMs(entries.find((entry) => entry.id === playoutId))
    return (emitted * 1000) / rate + (lost - this.holes) * PACKET_MS + this.shift
  }
}
