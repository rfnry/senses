export class Node {
  buffer: unknown = null
  onended: (() => void) | null = null
  started = -1
  stopped = false
  connect(): void {}
  start(at: number): void {
    this.started = at
  }
  stop(): void {
    this.stopped = true
  }
}

export class Context {
  static made: Context[] = []
  currentTime = 0
  destination = {} as AudioNode
  nodes: Node[] = []
  closed = false
  audioWorklet = { addModule: async (_url: string) => {} }
  constructor() {
    Context.made.push(this)
  }
  createBuffer(_channels: number, length: number, rate: number): AudioBuffer {
    return { duration: length / rate, getChannelData: () => new Float32Array(length) } as unknown as AudioBuffer
  }
  createBufferSource(): AudioBufferSourceNode {
    const node = new Node()
    this.nodes.push(node)
    return node as unknown as AudioBufferSourceNode
  }
  createMediaStreamSource(_stream: MediaStream): { connect(node: unknown): void } {
    return { connect() {} }
  }
  async close(): Promise<void> {
    this.closed = true
  }
}

export class Socket {
  static OPEN = 1
  static made: Socket[] = []
  readyState = 0
  binaryType = 'blob'
  sent: Array<string | Uint8Array> = []
  onopen: (() => void) | null = null
  onmessage: ((message: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    Socket.made.push(this)
  }
  send(data: string | Uint8Array): void {
    this.sent.push(data)
  }
  close(): void {
    if (this.readyState === 3) return
    this.readyState = 3
    this.onclose?.()
  }
  open(): void {
    this.readyState = 1
    this.onopen?.()
  }
  deliver(data: unknown): void {
    this.onmessage?.({ data: typeof data === 'string' || data instanceof ArrayBuffer ? data : JSON.stringify(data) })
  }
  texts(): unknown[] {
    return this.sent.filter((m): m is string => typeof m === 'string').map((m) => JSON.parse(m))
  }
  binary(): Uint8Array[] {
    return this.sent.filter((m): m is Uint8Array => typeof m !== 'string')
  }
}

export class Track {
  enabled = true
  stopped = false
  constraints: MediaTrackConstraints[] = []
  onended: (() => void) | null = null
  constructor(
    readonly kind: 'audio' | 'video',
    readonly source: 'microphone' | 'camera' | 'screen' = kind === 'audio' ? 'microphone' : 'camera',
  ) {}
  stop(): void {
    this.stopped = true
  }
  end(): void {
    this.stopped = true
    this.onended?.()
  }
  async applyConstraints(constraints: MediaTrackConstraints): Promise<void> {
    this.constraints.push(constraints)
  }
}

export class Stream {
  constructor(readonly tracks: Track[]) {}
  getTracks(): Track[] {
    return this.tracks
  }
  getAudioTracks(): Track[] {
    return this.tracks.filter((t) => t.kind === 'audio')
  }
  getVideoTracks(): Track[] {
    return this.tracks.filter((t) => t.kind === 'video')
  }
}

export class Port {
  onmessage: ((message: { data: ArrayBuffer }) => void) | null = null
  post(pcm: Uint8Array): void {
    this.onmessage?.({ data: pcm.slice().buffer })
  }
}

export class WorkletNode {
  static made: WorkletNode[] = []
  port = new Port()
  constructor(
    readonly context: unknown,
    readonly name: string,
    readonly options: unknown,
  ) {
    WorkletNode.made.push(this)
  }
  disconnect(): void {}
}

export const JPEG = Uint8Array.of(0xff, 0xd8, 0xff, 0xd9)

export function media() {
  const streams: Stream[] = []
  const asked: Array<{ source: 'microphone' | 'camera' | 'screen'; constraints: MediaStreamConstraints }> = []
  const refused = { screen: false }
  const made = (track: Track) => {
    const stream = new Stream([track])
    streams.push(stream)
    return stream as unknown as MediaStream
  }
  const getUserMedia = async (constraints: MediaStreamConstraints) => {
    asked.push({ source: constraints.video ? 'camera' : 'microphone', constraints })
    return made(constraints.video ? new Track('video') : new Track('audio'))
  }
  const getDisplayMedia = async (constraints: DisplayMediaStreamOptions) => {
    asked.push({ source: 'screen', constraints })
    if (refused.screen) throw new Error('NotAllowedError')
    return made(new Track('video', 'screen'))
  }
  const video = () => streams.flatMap((stream) => stream.getVideoTracks())
  return { streams, asked, refused, video, navigator: { mediaDevices: { getUserMedia, getDisplayMedia } } }
}

export function documentWithCanvas() {
  const video = {
    muted: false,
    playsInline: false,
    srcObject: null as unknown,
    videoWidth: 1280,
    videoHeight: 720,
    play: async () => {},
  }
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage() {} }),
    toBlob(callback: (blob: Blob | null) => void) {
      callback(new Blob([JPEG]))
    },
  }
  return { createElement: (tag: string) => (tag === 'video' ? video : canvas) }
}
