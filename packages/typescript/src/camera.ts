export type CameraOptions = { fps: number; width: number; quality: number }

export class Camera {
  private stream: MediaStream | null = null
  private video: HTMLVideoElement | null = null
  private canvas: HTMLCanvasElement | null = null
  private timer: ReturnType<typeof setInterval> | null = null
  private busy = false
  private paused = false
  private fps: number

  constructor(
    private readonly options: CameraOptions,
    private readonly send: (jpeg: Uint8Array) => void,
  ) {
    this.fps = options.fps
  }

  async start(): Promise<MediaStream> {
    const stream = await navigator.mediaDevices.getUserMedia({ video: true })
    this.stream = stream
    const video = document.createElement('video')
    video.muted = true
    video.playsInline = true
    video.srcObject = stream
    await video.play()
    this.video = video
    this.canvas = document.createElement('canvas')
    this.schedule()
    return stream
  }

  pause(): void {
    this.paused = true
    for (const track of this.stream?.getVideoTracks() ?? []) track.enabled = false
  }

  resume(): void {
    this.paused = false
    for (const track of this.stream?.getVideoTracks() ?? []) track.enabled = true
  }

  setFps(fps: number): void {
    this.fps = fps
    this.schedule()
  }

  stop(): void {
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
    for (const track of this.stream?.getTracks() ?? []) track.stop()
    this.stream = null
  }

  private schedule(): void {
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = setInterval(() => void this.capture(), 1000 / this.fps)
  }

  private async capture(): Promise<void> {
    const { video, canvas } = this
    if (this.busy || this.paused || video === null || canvas === null || video.videoWidth === 0) return
    this.busy = true
    try {
      const scale = Math.min(1, this.options.width / video.videoWidth)
      canvas.width = Math.round(video.videoWidth * scale)
      canvas.height = Math.round(video.videoHeight * scale)
      canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, 'image/jpeg', this.options.quality),
      )
      if (blob !== null && !this.paused) this.send(new Uint8Array(await blob.arrayBuffer()))
    } finally {
      this.busy = false
    }
  }
}
