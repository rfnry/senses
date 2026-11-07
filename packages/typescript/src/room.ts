import type { Camera, CameraChoice, CameraOptions, VideoSource } from './camera.js'
import { SensesException } from './errors.js'
import type { SensesEvent } from './wire.js'

export type ConnectOptions = {
  url: string
  transport: 'websocket' | 'webrtc'
  protocols?: string | string[]
  headers?: Record<string, string>
  microphone?: boolean
  camera?:
    | false
    | ({ source?: VideoSource; fps?: number; width?: number; quality?: number } & CameraChoice)
  sampleRate?: number
  iceServers?: RTCIceServer[]
  signal?: AbortSignal
}

export const ABORTED = 'senses: connect was aborted'

export type SensesStatus = 'open' | 'closed'

export type SensesAudio = 'playing' | 'blocked'

export type SensesListener = (
  event:
    | SensesEvent
    | { kind: 'status'; status: SensesStatus }
    | { kind: 'audio'; status: SensesAudio }
    | { kind: 'video'; source: VideoSource | null }
) => void

export interface Senses {
  readonly status: SensesStatus
  readonly audio: SensesAudio
  subscribe(listener: SensesListener): () => void
  resumeAudio(): Promise<void>
  mute(): void
  unmute(): void
  shareScreen(): Promise<void>
  useCamera(choice?: CameraChoice): Promise<void>
  pauseCamera(): void
  resumeCamera(): void
  setFps(fps: number): void
  close(): Promise<void>
}

export function cameraOptions(
  camera: { source?: VideoSource; fps?: number; width?: number; quality?: number } & CameraChoice
): CameraOptions {
  return {
    source: camera.source ?? 'camera',
    fps: camera.fps ?? 1,
    width: camera.width ?? 640,
    quality: camera.quality ?? 0.7,
    choice: checkChoice(camera),
  }
}

function checkChoice(choice: CameraChoice): CameraChoice {
  if (choice.facing !== undefined && choice.deviceId !== undefined)
    throw new SensesException('senses: the camera takes facing or deviceId, not both')
  return choice
}

function checkCamera<T>(camera: T | null, verb: string): T {
  if (camera === null) throw new SensesException(`senses: ${verb} needs the camera option`)
  return camera
}

export function checkFps(fps: number, camera: unknown): void {
  if (!(fps > 0)) throw new SensesException('senses: fps must be positive')
  if (camera === null) throw new SensesException('senses: setFps needs a camera')
}

export class Listeners {
  status: SensesStatus = 'open'
  audio: SensesAudio = 'playing'
  private readonly listeners = new Set<SensesListener>()

  subscribe(listener: SensesListener): () => void {
    this.listeners.add(listener)
    listener({ kind: 'status', status: this.status })
    if (this.audio === 'blocked') listener({ kind: 'audio', status: 'blocked' })
    return () => this.listeners.delete(listener)
  }

  tell(event: Parameters<SensesListener>[0]): void {
    for (const listener of this.listeners) {
      try {
        listener(event)
      } catch (error) {
        setTimeout(() => {
          throw error
        })
      }
    }
  }

  hearing(audio: SensesAudio): void {
    if (audio === this.audio) return
    this.audio = audio
    this.tell({ kind: 'audio', status: audio })
  }

  end(): void {
    if (this.status === 'closed') return
    this.status = 'closed'
    this.tell({ kind: 'status', status: 'closed' })
  }
}

export class Ending {
  private stopping = false
  private released: Promise<unknown> = Promise.resolve()
  private refuse: (reason: SensesException) => void = () => {}
  private readonly gone = new Promise<never>((_, reject) => {
    this.refuse = reject
  })

  constructor(
    private readonly room: Listeners,
    private readonly release: () => Promise<unknown>
  ) {}

  get stopped(): boolean {
    return this.stopping
  }

  async stop(): Promise<void> {
    if (!this.stopping) {
      this.stopping = true
      this.released = this.release()
      this.room.end()
    }
    await this.released
  }

  lost(message = 'transport: the connection closed before it was ready'): void {
    this.refuse(new SensesException(message))
    void this.stop()
  }

  tell(event: SensesEvent): void {
    this.room.tell(event)
    if (event.kind === 'closed') this.lost(event.message ?? undefined)
  }

  async until<T>(connecting: Promise<T>, signal?: AbortSignal): Promise<T> {
    const abort = () => this.lost(ABORTED)
    signal?.addEventListener('abort', abort, { once: true })
    try {
      return await Promise.race([connecting, this.gone])
    } finally {
      signal?.removeEventListener('abort', abort)
    }
  }
}

export function handle(
  room: Listeners,
  ending: Ending,
  camera: Camera | null,
  controls: Pick<
    Senses,
    'mute' | 'unmute' | 'pauseCamera' | 'resumeCamera' | 'setFps' | 'resumeAudio'
  >
): Senses {
  return {
    get status() {
      return room.status
    },
    get audio() {
      return room.audio
    },
    subscribe: (listener) => room.subscribe(listener),
    async shareScreen() {
      await checkCamera(camera, 'shareScreen').use('screen')
    },
    async useCamera(choice) {
      await checkCamera(camera, 'useCamera').use('camera', choice && checkChoice(choice))
    },
    close: () => ending.stop(),
    ...controls,
  }
}
