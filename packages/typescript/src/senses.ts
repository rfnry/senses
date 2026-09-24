import { Camera } from './camera.js'
import { Microphone } from './microphone.js'
import { Playback } from './playback.js'
import { type ConnectOptions, cameraOptions, checkFps, Listeners, type Senses } from './room.js'
import { overWebRTC } from './webrtc.js'
import { WebSocketLink } from './websocket.js'

export function connect(options: ConnectOptions): Promise<Senses> {
  return options.transport === 'webrtc' ? overWebRTC(options) : overWebSocket(options)
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
