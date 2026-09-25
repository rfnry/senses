export type VideoSource = 'camera' | 'screen'

export type CameraOptions = { source: VideoSource; fps: number; width: number; quality: number }

function capture(source: VideoSource, width: number, fps: number): Promise<MediaStream> {
  const video = { width, frameRate: fps }
  return source === 'screen'
    ? navigator.mediaDevices.getDisplayMedia({ video, audio: false })
    : navigator.mediaDevices.getUserMedia({ video })
}

export class Camera {
  source: VideoSource | null = null
  fps: number
  private stream: MediaStream | null = null
  private back: VideoSource | null = null
  private paused = false
  private stopped = false

  constructor(
    private readonly options: CameraOptions,
    private readonly show: (stream: MediaStream | null) => Promise<void>,
    private readonly told: (source: VideoSource | null) => void,
  ) {
    this.fps = options.fps
  }

  get track(): MediaStreamTrack | null {
    return this.stream?.getVideoTracks()[0] ?? null
  }

  async use(source: VideoSource): Promise<void> {
    const stream = await capture(source, this.options.width, this.fps)
    if (this.stopped) {
      for (const track of stream.getTracks()) track.stop()
      return
    }
    for (const track of stream.getVideoTracks()) {
      track.enabled = !this.paused
      track.onended = () => void this.ended(stream)
    }
    if (source === 'screen' && this.source !== 'screen') this.back = this.source
    const previous = this.stream
    this.stream = stream
    this.source = source
    await this.show(stream)
    for (const track of previous?.getTracks() ?? []) track.stop()
    this.told(source)
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
    for (const track of this.stream?.getVideoTracks() ?? []) void track.applyConstraints({ frameRate: fps })
  }

  stop(): void {
    this.stopped = true
    for (const track of this.stream?.getTracks() ?? []) track.stop()
    this.stream = null
    this.source = null
  }

  private async ended(stream: MediaStream): Promise<void> {
    if (stream !== this.stream || this.stopped) return
    if (this.source === 'screen' && this.back === 'camera') {
      const returned = await this.use('camera').then(
        () => true,
        () => false,
      )
      if (returned) return
    }
    if (stream !== this.stream) return
    for (const track of stream.getTracks()) track.stop()
    this.stream = null
    this.source = null
    await this.show(null)
    this.told(null)
  }
}
