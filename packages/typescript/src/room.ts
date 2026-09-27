import type { CameraChoice, CameraOptions, VideoSource } from './camera.js'
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

export function checkChoice(choice: CameraChoice): CameraChoice {
  if (choice.facing !== undefined && choice.deviceId !== undefined)
    throw new SensesException('senses: the camera takes facing or deviceId, not both')
  return choice
}

export function checkCamera<T>(camera: T | null, verb: string): T {
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
    for (const listener of this.listeners) listener(event)
  }

  end(): void {
    if (this.status === 'closed') return
    this.status = 'closed'
    this.tell({ kind: 'status', status: 'closed' })
  }
}
