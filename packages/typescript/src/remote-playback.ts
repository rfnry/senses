type Counts = { emitted: number; concealed: number; rate: number }

const POLL_MS = 100

export class RemotePlayback {
  private element: HTMLAudioElement | null = null
  private receiver: RTCRtpReceiver | null = null
  private fenced = 0
  private readonly baselines = new Map<number, Promise<Counts | null>>()
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
    if (utterance <= this.fenced || this.baselines.has(utterance) || this.reported.has(utterance)) return
    if (this.element !== null) this.element.muted = false
    this.baselines.set(utterance, this.counts())
  }

  async stop(utterance: number): Promise<void> {
    this.fenced = Math.max(this.fenced, utterance)
    if (this.element !== null) this.element.muted = true
    if (!this.baselines.has(utterance) && !this.reported.has(utterance)) {
      this.reported.add(utterance)
      this.report(utterance, 0)
    }
    const ending = [...this.baselines.keys()].filter((number) => number <= utterance).sort((a, b) => a - b)
    for (const number of ending) await this.finish(number)
  }

  async silent(utterance: number): Promise<void> {
    let last: number | null = null
    while (this.baselines.has(utterance)) {
      const now = await this.counts()
      if (now === null || now.emitted === last) return this.finish(utterance)
      last = now.emitted
      await new Promise((resolve) => setTimeout(resolve, POLL_MS))
    }
  }

  close(): void {
    this.element?.pause()
    if (this.element !== null) this.element.srcObject = null
    this.element = null
    this.receiver = null
  }

  private async finish(utterance: number): Promise<void> {
    const baseline = this.baselines.get(utterance)
    if (baseline === undefined) return
    this.baselines.delete(utterance)
    this.reported.add(utterance)
    const [before, now] = [await baseline, await this.counts()]
    if (before === null || now === null) return this.report(utterance, 0)
    const samples = Math.max(0, now.emitted - before.emitted) - Math.max(0, now.concealed - before.concealed)
    this.report(utterance, Math.max(0, Math.round((samples / now.rate) * 1000)))
  }

  private async counts(): Promise<Counts | null> {
    const stats = await this.receiver?.getStats().catch(() => null)
    if (stats === null || stats === undefined) return null
    const entries = Array.from(stats.values()) as Array<Record<string, unknown>>
    const inbound = entries.find((entry) => entry.type === 'inbound-rtp')
    if (inbound === undefined) return null
    const { jitterBufferEmittedCount: emitted, concealedSamples: concealed, codecId } = inbound
    if (typeof emitted !== 'number' || typeof concealed !== 'number') return null
    const codec = entries.find((entry) => entry.id === codecId)
    return { emitted, concealed, rate: typeof codec?.clockRate === 'number' ? codec.clockRate : 48000 }
  }
}
