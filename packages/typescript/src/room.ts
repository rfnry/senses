import type { CameraOptions } from './camera.js'
import { SensesError } from './errors.js'
import type { SensesEvent } from './wire.js'

export type ConnectOptions = {
  url: string
  transport: 'websocket' | 'webrtc'
  protocols?: string | string[]
  headers?: Record<string, string>
  microphone?: boolean
  camera?: false | { fps?: number; width?: number; quality?: number }
  sampleRate?: number
  iceServers?: RTCIceServer[]
}

export type SensesStatus = 'open' | 'closed'

export type SensesListener = (event: SensesEvent | { kind: 'status'; status: SensesStatus }) => void

export interface Senses {
  readonly status: SensesStatus
  subscribe(listener: SensesListener): () => void
  mute(): void
  unmute(): void
  pauseCamera(): void
  resumeCamera(): void
  setFps(fps: number): void
  close(): Promise<void>
}

export function cameraOptions(camera: { fps?: number; width?: number; quality?: number }): CameraOptions {
  return { fps: camera.fps ?? 1, width: camera.width ?? 640, quality: camera.quality ?? 0.7 }
}

export function checkFps(fps: number, camera: unknown): void {
  if (!(fps > 0)) throw new SensesError('senses: fps must be positive')
  if (camera === null) throw new SensesError('senses: setFps needs a camera')
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
