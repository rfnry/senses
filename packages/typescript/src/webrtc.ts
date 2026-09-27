import { Camera } from './camera.js'
import { SensesException } from './errors.js'
import { MICROPHONE } from './microphone.js'
import { RemotePlayback } from './remote-playback.js'
import {
  type ConnectOptions,
  cameraOptions,
  checkCamera,
  checkChoice,
  checkFps,
  Listeners,
  type Senses,
} from './room.js'
import { hello, parseControl, played } from './wire.js'

function gathered(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    pc.addEventListener('icegatheringstatechange', () => {
      if (pc.iceGatheringState === 'complete') resolve()
    })
  })
}

export async function overWebRTC(options: ConnectOptions): Promise<Senses> {
  const settings = options.camera ? cameraOptions(options.camera) : null
  const room = new Listeners()
  const pc = new RTCPeerConnection({ iceServers: options.iceServers ?? [] })
  const channel = pc.createDataChannel('senses', { ordered: true })
  const remote = new RemotePlayback((utterance, ms) => {
    if (channel.readyState === 'open') channel.send(played(utterance, ms))
  })
  const streams: MediaStream[] = []
  const microphone = () => streams.flatMap((stream) => stream.getAudioTracks())
  let sender: RTCRtpSender | null = null
  const camera = settings
    ? new Camera(
        settings,
        async (stream) => {
          await sender?.replaceTrack(camera?.paused ? null : (stream?.getVideoTracks()[0] ?? null))
        },
        (source) => room.tell({ kind: 'video', source })
      )
    : null
  const ready = new Promise<void>((resolve, reject) => {
    let open = false
    channel.onopen = () => channel.send(hello())
    channel.onmessage = (message: MessageEvent) => {
      const frame = parseControl(String(message.data))
      if (!open) {
        if (frame.type === 'ready') {
          open = true
          resolve()
        } else if (frame.type === 'error') reject(new SensesException(frame.message))
        return
      }
      if (frame.type === 'speaking') remote.speaking(frame.utterance)
      else if (frame.type === 'silent') void remote.silent(frame.utterance)
      else if (frame.type === 'stop') void remote.stop(frame.utterance)
      else if (frame.type === 'event') {
        room.tell(frame.event)
        if (frame.event.kind === 'closed') room.end()
      }
    }
    channel.onclose = () => {
      if (!open) reject(new SensesException('transport: the connection closed before it was ready'))
      room.end()
    }
  })
  ready.catch(() => {})
  const stop = () => {
    for (const stream of streams) for (const track of stream.getTracks()) track.stop()
    camera?.stop()
    remote.close()
    pc.close()
    room.end()
  }
  try {
    if (options.microphone === false) pc.addTransceiver('audio', { direction: 'recvonly' })
    else {
      const microphone = await navigator.mediaDevices.getUserMedia({ audio: MICROPHONE })
      streams.push(microphone)
      for (const track of microphone.getAudioTracks()) pc.addTrack(track, microphone)
    }
    if (camera !== null && settings !== null) {
      await camera.use(settings.source)
      sender = pc.addTransceiver(camera.track ?? 'video', {
        direction: 'sendonly',
        sendEncodings: [{ maxFramerate: settings.fps }],
      }).sender
    }
    pc.ontrack = (event: RTCTrackEvent) =>
      remote.start(event.streams[0] ?? new MediaStream([event.track]), event.receiver)
    await pc.setLocalDescription(await pc.createOffer())
    await gathered(pc)
    const response = await fetch(options.url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...options.headers },
      body: JSON.stringify({ sdp: pc.localDescription?.sdp }),
      credentials: 'same-origin',
    })
    if (!response.ok)
      throw new SensesException(`webrtc: the offer was refused with status ${response.status}`)
    const answer = (await response.json()) as { sdp: string }
    await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp })
    await ready
  } catch (error) {
    stop()
    throw error
  }
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'failed' || pc.connectionState === 'closed') room.end()
  }
  const enable = (on: boolean) => {
    for (const track of microphone()) track.enabled = on
  }
  return {
    get status() {
      return room.status
    },
    subscribe: (listener) => room.subscribe(listener),
    mute: () => enable(false),
    unmute: () => enable(true),
    async shareScreen() {
      await checkCamera(camera, 'shareScreen').use('screen')
    },
    async useCamera(choice) {
      await checkCamera(camera, 'useCamera').use('camera', choice && checkChoice(choice))
    },
    pauseCamera() {
      camera?.pause()
      if (room.status === 'open') void sender?.replaceTrack(null)
    },
    resumeCamera() {
      camera?.resume()
      if (room.status === 'open') void sender?.replaceTrack(camera?.track ?? null)
    },
    setFps(fps) {
      checkFps(fps, camera)
      camera?.setFps(fps)
      if (sender === null || room.status === 'closed') return
      const parameters = sender.getParameters()
      for (const encoding of parameters.encodings) encoding.maxFramerate = fps
      void sender.setParameters(parameters)
    },
    async close() {
      stop()
    },
  }
}
