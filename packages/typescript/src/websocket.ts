import { SensesException } from './errors.js'
import {
  type AudioOut,
  decodeAudio,
  encodeAudio,
  encodeImage,
  hello,
  parseControl,
  played,
  type ServerControl,
} from './wire.js'

export type LinkHandlers = {
  control(frame: ServerControl): void
  audio(frame: AudioOut): void
  closed(): void
}

export class WebSocketLink {
  readonly ready: Promise<void>
  private readonly socket: WebSocket

  constructor(url: string, protocols: string | string[] | undefined, handlers: LinkHandlers) {
    const socket = new WebSocket(url, protocols)
    socket.binaryType = 'arraybuffer'
    this.socket = socket
    this.ready = new Promise((resolve, reject) => {
      socket.onopen = () => socket.send(hello())
      socket.onmessage = (message: MessageEvent) => {
        if (message.data instanceof ArrayBuffer) return handlers.audio(decodeAudio(message.data))
        const frame = parseControl(String(message.data))
        if (frame.type === 'ready') resolve()
        else if (frame.type === 'error') reject(new SensesException(frame.message))
        else handlers.control(frame)
      }
      socket.onclose = () => handlers.closed()
    })
  }

  sendAudio(pcm: Uint8Array, sampleRate: number): void {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(encodeAudio(pcm, sampleRate))
  }

  sendImage(jpeg: Uint8Array): void {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(encodeImage(jpeg))
  }

  played(utterance: number, ms: number): void {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(played(utterance, ms))
  }

  close(): void {
    this.socket.close()
  }
}
