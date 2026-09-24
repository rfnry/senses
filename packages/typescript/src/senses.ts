import { Camera, type CameraOptions } from './camera.js'
import { SensesError } from './errors.js'
import { Microphone } from './microphone.js'
import { Playback } from './playback.js'
import { WebSocketLink } from './websocket.js'
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

export function connect(options: ConnectOptions): Promise<Senses> {
  if (options.transport === 'webrtc') return Promise.reject(new SensesError('senses: webrtc is not built yet'))
  return overWebSocket(options)
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

async function overWebSocket(options: ConnectOptions): Promise<Senses> {
  const room = new Listeners()
  const rate = options.sampleRate ?? 16000
  let report = (_utterance: number, _ms: number) => {}
  const playback = new Playback(new AudioContext(), (utterance, ms) => report(utterance, ms))
  const link = await WebSocketLink.open(options.url, options.protocols, {
    control(frame) {
      if (frame.type === 'speaking') playback.speaking(frame.utterance)
      else if (frame.type === 'silent') playback.silent(frame.utterance)
      else if (frame.type === 'stop') playback.stop(frame.utterance)
      else if (frame.type === 'event') {
        room.tell(frame.event)
        if (frame.event.kind === 'closed') room.end()
      }
    },
    audio(frame) {
      playback.push(frame)
    },
    closed: () => room.end(),
  })
  report = (utterance, ms) => link.played(utterance, ms)
  const microphone = options.microphone === false ? null : new Microphone()
  microphone?.onChunk((pcm) => link.sendAudio(pcm, rate))
  await microphone?.start(rate)
  const camera = options.camera ? new Camera(cameraOptions(options.camera), (jpeg) => link.sendImage(jpeg)) : null
  await camera?.start()
  return {
    get status() {
      return room.status
    },
    subscribe: (listener) => room.subscribe(listener),
    mute() {
      if (microphone !== null) microphone.enabled = false
    },
    unmute() {
      if (microphone !== null) microphone.enabled = true
    },
    pauseCamera: () => camera?.pause(),
    resumeCamera: () => camera?.resume(),
    setFps(fps) {
      checkFps(fps, camera)
      camera?.setFps(fps)
    },
    async close() {
      camera?.stop()
      await microphone?.stop()
      link.close()
      await playback.close()
      room.end()
    },
  }
}
