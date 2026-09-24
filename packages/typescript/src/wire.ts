import { SensesError } from './errors.js'

export const PROTOCOL = 1
const AUDIO = 1
const IMAGE = 2
const HEADER = 10

export type Identity = {
  role: 'user' | 'assistant' | 'system'
  id: string
  name: string | null
  metadata: Record<string, unknown>
}

type Stamp = { scope: string; at: string }

export type SensesEvent =
  | (Stamp & { kind: 'heard'; text: string; final: boolean; speaker: Identity })
  | (Stamp & { kind: 'turn_ended'; text: string; speaker: Identity; probability: number | null })
  | (Stamp & { kind: 'said'; text: string; heard: string; interrupted: boolean; triggered_by: Identity | null })
  | (Stamp & { kind: 'interrupted'; heard: string; triggered_by: Identity })
  | (Stamp & { kind: 'speaking' })
  | (Stamp & { kind: 'silent' })
  | (Stamp & { kind: 'looked'; frames: number; triggered_by: Identity })
  | (Stamp & { kind: 'muted'; muted: boolean; triggered_by: Identity })
  | (Stamp & { kind: 'closed'; reason: 'closed' | 'hung_up' | 'failed'; message: string | null })

export type ServerControl =
  | { type: 'ready' }
  | { type: 'speaking' | 'silent' | 'stop'; utterance: number }
  | { type: 'event'; event: SensesEvent }
  | { type: 'error'; message: string }

export type AudioOut = { utterance: number; sampleRate: number; channels: number; pcm: Uint8Array }

const CONTROLS = new Set(['ready', 'speaking', 'silent', 'stop', 'event', 'error'])

export function hello(): string {
  return JSON.stringify({ type: 'hello', protocol: PROTOCOL })
}

export function played(utterance: number, ms: number): string {
  return JSON.stringify({ type: 'played', utterance, ms: Math.max(0, Math.round(ms)) })
}

export function encodeAudio(pcm: Uint8Array, sampleRate: number, channels = 1, utterance = 0): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(HEADER + pcm.byteLength)
  const view = new DataView(out.buffer)
  view.setUint8(0, AUDIO)
  view.setUint8(1, channels)
  view.setUint32(2, sampleRate, true)
  view.setUint32(6, utterance, true)
  out.set(pcm, HEADER)
  return out
}

export function encodeImage(jpeg: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(1 + jpeg.byteLength)
  out[0] = IMAGE
  out.set(jpeg, 1)
  return out
}

export function decodeAudio(data: ArrayBuffer): AudioOut {
  const view = new DataView(data)
  if (data.byteLength < HEADER || view.getUint8(0) !== AUDIO) throw new SensesError('wire: not an audio frame')
  return {
    channels: view.getUint8(1),
    sampleRate: view.getUint32(2, true),
    utterance: view.getUint32(6, true),
    pcm: new Uint8Array(data, HEADER),
  }
}

export function parseControl(text: string): ServerControl {
  const frame: unknown = JSON.parse(text)
  const type = typeof frame === 'object' && frame !== null ? (frame as { type?: unknown }).type : undefined
  if (typeof type !== 'string' || !CONTROLS.has(type))
    throw new SensesError(`wire: unknown control frame ${String(type)}`)
  return frame as ServerControl
}
