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
}

export type SensesStatus = 'open' | 'closed'

export type SensesListener = (
  event:
    | SensesEvent
    | { kind: 'status'; status: SensesStatus }
    | { kind: 'video'; source: VideoSource | null }
) => void

export interface Senses {
  readonly status: SensesStatus
  subscribe(listener: SensesListener): () => void
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
  private readonly listeners = new Set<SensesListener>()

  subscribe(listener: SensesListener): () => void {
    this.listeners.add(listener)
    listener({ kind: 'status', status: this.status })
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
    if (event.kind === 'closed') this.lost()
  }

  until<T>(connecting: Promise<T>): Promise<T> {
    return Promise.race([connecting, this.gone])
  }
}

export function handle(
  room: Listeners,
  ending: Ending,
  camera: Camera | null,
  controls: Pick<Senses, 'mute' | 'unmute' | 'pauseCamera' | 'resumeCamera' | 'setFps'>
): Senses {
  return {
    get status() {
      return room.status
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
