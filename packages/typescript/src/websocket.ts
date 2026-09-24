import { SensesError } from './errors.js'
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

export type LinkHandlers = { control(frame: ServerControl): void; audio(frame: AudioOut): void; closed(): void }

export class WebSocketLink {
  private constructor(private readonly socket: WebSocket) {}

  static open(url: string, protocols: string | string[] | undefined, handlers: LinkHandlers): Promise<WebSocketLink> {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url, protocols)
      socket.binaryType = 'arraybuffer'
      let ready = false
      socket.onopen = () => socket.send(hello())
      socket.onmessage = (message: MessageEvent) => {
        if (message.data instanceof ArrayBuffer) return handlers.audio(decodeAudio(message.data))
        const frame = parseControl(String(message.data))
        if (!ready && frame.type === 'ready') {
          ready = true
          resolve(new WebSocketLink(socket))
        } else if (!ready && frame.type === 'error') {
          reject(new SensesError(frame.message))
        } else {
          handlers.control(frame)
        }
      }
      socket.onclose = () => {
        if (!ready) reject(new SensesError('transport: the connection closed before it was ready'))
        handlers.closed()
      }
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
