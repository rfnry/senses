type Spoken = { start: number; end: number | null }

const POLL_MS = 100
const FADE_MS = 60
const FADE_STEPS = 6
const PACKET_MS = 20
const STALL_POLLS = 20
const TAIL_MS = 60

const counted = (packets: unknown) => (typeof packets === 'number' ? Math.max(0, packets) : 0)

export class RemotePlayback {
  private element: HTMLAudioElement | null = null
  private receiver: RTCRtpReceiver | null = null
  private fenced = 0
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

  speaking(utterance: number, sentMs: number): void {
    if (utterance <= this.fenced || this.spoken.has(utterance) || this.reported.has(utterance))
      return
    this.stopFading()
    if (this.element !== null) {
      this.element.volume = 1
      this.element.muted = false
    }
    for (const earlier of this.spoken.values()) earlier.end ??= sentMs
    this.spoken.set(utterance, { start: sentMs, end: null })
  }

  async stop(utterance: number): Promise<void> {
    this.fenced = Math.max(this.fenced, utterance)
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

  async silent(utterance: number, sentMs: number): Promise<void> {
    const spoken = this.spoken.get(utterance)
    if (spoken === undefined) return
    spoken.end ??= sentMs
    const end = spoken.end
    let last: number | null = null
    let idle = 0
    while (this.spoken.has(utterance)) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS))
      const now = await this.position()
      if (!this.spoken.has(utterance)) return
      if (now === null) return this.finish(utterance)
      idle = now === last ? idle + 1 : 0
      if (now >= end || (idle > 0 && end - now <= TAIL_MS)) return this.finish(utterance, true)
      if (idle >= STALL_POLLS) return this.finish(utterance)
      last = now
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

  private async finish(utterance: number, whole = false): Promise<void> {
    const spoken = this.spoken.get(utterance)
    if (spoken === undefined) return
    this.spoken.delete(utterance)
    this.reported.add(utterance)
    const { start, end } = spoken
    if (whole && end !== null) return this.report(utterance, end - start)
    const now = await this.position()
    if (now === null) return this.report(utterance, 0)
    this.report(utterance, Math.max(0, Math.round(Math.min(now, end ?? now) - start)))
  }

  private async position(): Promise<number | null> {
    const stats = await this.receiver?.getStats().catch(() => null)
    if (stats === null || stats === undefined) return null
    const entries = Array.from(stats.values()) as Array<Record<string, unknown>>
    const inbound = entries.find((entry) => entry.type === 'inbound-rtp')
    if (inbound === undefined) return 0
    const { jitterBufferEmittedCount: emitted, codecId } = inbound
    if (typeof emitted !== 'number') return null
    const codec = entries.find((entry) => entry.id === codecId)
    const rate = typeof codec?.clockRate === 'number' ? codec.clockRate : 48000
    const concealed = counted(inbound.packetsLost) + counted(inbound.packetsDiscarded)
    return (emitted * 1000) / rate + concealed * PACKET_MS
  }
}
