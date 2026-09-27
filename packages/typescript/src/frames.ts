export class Frames {
  private stream: MediaStream | null = null
  private video: HTMLVideoElement | null = null
  private canvas: HTMLCanvasElement | null = null
  private timer: ReturnType<typeof setInterval> | null = null
  private busy = false

  constructor(
    private readonly options: { fps: number; width: number; quality: number },
    private readonly send: (jpeg: Uint8Array) => void,
  ) {}

  async show(stream: MediaStream | null): Promise<void> {
    this.stream = stream
    if (this.video === null) {
      this.video = document.createElement('video')
      this.video.muted = true
      this.video.playsInline = true
      this.canvas = document.createElement('canvas')
    }
    this.video.srcObject = stream
    if (stream !== null) await this.video.play()
    if (this.timer === null) this.schedule()
  }

  setFps(fps: number): void {
    this.options.fps = fps
    if (this.timer !== null) this.schedule()
  }

  stop(): void {
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
    this.stream = null
  }

  private live(): boolean {
    return this.stream?.getVideoTracks().some((track) => track.enabled) ?? false
  }

  private schedule(): void {
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = setInterval(() => void this.capture(), 1000 / this.options.fps)
  }

  private async capture(): Promise<void> {
    const { video, canvas } = this
    if (this.busy || !this.live() || video === null || canvas === null || video.videoWidth === 0) return
    this.busy = true
    try {
      const scale = Math.min(1, this.options.width / video.videoWidth)
      canvas.width = Math.round(video.videoWidth * scale)
      canvas.height = Math.round(video.videoHeight * scale)
      canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, 'image/jpeg', this.options.quality),
      )
      if (blob !== null && this.live()) this.send(new Uint8Array(await blob.arrayBuffer()))
    } finally {
      this.busy = false
    }
  }
}
