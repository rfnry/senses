type Counts = { emitted: number; arrived: number }
type Spoken = {
  start: Promise<number | null>
  end: Promise<number | null> | null
  slack: number
}

const POLL_MS = 100
const FADE_MS = 60
const FADE_STEPS = 6
const PACKET_MS = 20
const STALL_POLLS = 20

const counted = (packets: unknown) => (typeof packets === 'number' ? Math.max(0, packets) : 0)

export class RemotePlayback {
  private element: HTMLAudioElement | null = null
  private receiver: RTCRtpReceiver | null = null
  private fenced = 0
  private rest: Counts = { emitted: 0, arrived: 0 }
  private quiet = true
  private fading: ReturnType<typeof setInterval> | null = null
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

  speaking(utterance: number): void {
    if (utterance <= this.fenced || this.spoken.has(utterance) || this.reported.has(utterance))
      return
    this.stopFading()
    if (this.element !== null) {
      this.element.volume = 1
      this.element.muted = false
    }
    const start = this.quiet ? Promise.resolve(this.rest.emitted) : this.edge()
    for (const earlier of this.spoken.values()) earlier.end ??= start
    this.spoken.set(utterance, { start, end: null, slack: this.quiet ? 0 : PACKET_MS })
    this.quiet = false
  }

  async stop(utterance: number): Promise<void> {
    this.fenced = Math.max(this.fenced, utterance)
    this.quiet = false
    this.fadeOut()
    if (!this.spoken.has(utterance) && !this.reported.has(utterance)) {
      this.reported.add(utterance)
      this.report(utterance, 0)
    }
    const ending = [...this.spoken.keys()]
      .filter((number) => number <= utterance)
      .sort((a, b) => a - b)
    for (const number of ending) await this.finish(number)
  }

  async silent(utterance: number): Promise<void> {
    let last: number | null = null
    let idle = 0
    while (this.spoken.has(utterance)) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS))
      const [now, end] = await Promise.all([this.counts(), this.spoken.get(utterance)?.end])
      const spoken = this.spoken.get(utterance)
      if (spoken === undefined) return
      if (now === null) return this.finish(utterance)
      idle = now.emitted === last ? idle + 1 : 0
      const stalled = idle > 0 && (now.emitted >= this.position(now) || idle >= STALL_POLLS)
      if (stalled) {
        this.rest = now
        this.quiet = this.spoken.size === 1
      }
      if (stalled || (typeof end === 'number' && now.emitted >= end))
        return this.finish(utterance, spoken.slack)
      last = now.emitted
    }
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

  private position(now: Counts): number {
    return this.rest.emitted + now.arrived - this.rest.arrived
  }

  private async edge(): Promise<number | null> {
    const now = await this.counts()
    return now && this.position(now)
  }

  private async finish(utterance: number, slack = 0): Promise<void> {
    const spoken = this.spoken.get(utterance)
    if (spoken === undefined) return
    this.spoken.delete(utterance)
    this.reported.add(utterance)
    const [start, end, now] = await Promise.all([spoken.start, spoken.end, this.counts()])
    if (start === null || now === null) return this.report(utterance, 0)
    const played = Math.min(now.emitted, end ?? now.emitted) - start
    this.report(utterance, played > 0 ? Math.round(played + slack) : 0)
  }

  private async counts(): Promise<Counts | null> {
    const stats = await this.receiver?.getStats().catch(() => null)
    if (stats === null || stats === undefined) return null
    const entries = Array.from(stats.values()) as Array<Record<string, unknown>>
    const inbound = entries.find((entry) => entry.type === 'inbound-rtp')
    if (inbound === undefined) return { emitted: 0, arrived: 0 }
    const { jitterBufferEmittedCount: emitted, packetsReceived: packets, codecId } = inbound
    if (typeof emitted !== 'number' || typeof packets !== 'number') return null
    const codec = entries.find((entry) => entry.id === codecId)
    const rate = typeof codec?.clockRate === 'number' ? codec.clockRate : 48000
    const lost = counted(inbound.packetsLost)
    const concealed = lost + counted(inbound.packetsDiscarded)
    return {
      emitted: (emitted * 1000) / rate + concealed * PACKET_MS,
      arrived: (packets + lost) * PACKET_MS,
    }
  }
}
