import { Camera } from './camera.js'
import { Frames } from './frames.js'
import { Microphone } from './microphone.js'
import { Playback } from './playback.js'
import {
  type ConnectOptions,
  cameraOptions,
  checkCamera,
  checkChoice,
  checkFps,
  Listeners,
  type Senses,
} from './room.js'
import { overWebRTC } from './webrtc.js'
import { WebSocketLink } from './websocket.js'

export function connect(options: ConnectOptions): Promise<Senses> {
  return options.transport === 'webrtc' ? overWebRTC(options) : overWebSocket(options)
}

async function overWebSocket(options: ConnectOptions): Promise<Senses> {
  const settings = options.camera ? cameraOptions(options.camera) : null
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
  const frames = settings ? new Frames({ ...settings }, (jpeg) => link.sendImage(jpeg)) : null
  const camera =
    settings && frames
      ? new Camera(
          settings,
          (stream) => frames.show(stream),
          (source) => room.tell({ kind: 'video', source }),
        )
      : null
  if (camera !== null && settings !== null) await camera.use(settings.source)
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
    async shareScreen() {
      await checkCamera(camera, 'shareScreen').use('screen')
    },
    async useCamera(choice) {
      await checkCamera(camera, 'useCamera').use('camera', choice && checkChoice(choice))
    },
    pauseCamera: () => camera?.pause(),
    resumeCamera: () => camera?.resume(),
    setFps(fps) {
      checkFps(fps, camera)
      camera?.setFps(fps)
      frames?.setFps(fps)
    },
    async close() {
      camera?.stop()
      frames?.stop()
      await microphone?.stop()
      link.close()
      await playback.close()
      room.end()
    },
  }
}
