import { Camera } from './camera.js'
import { SensesException } from './errors.js'
import { MICROPHONE } from './microphone.js'
import { RemotePlayback } from './remote-playback.js'
import {
  type ConnectOptions,
  cameraOptions,
  checkFps,
  Ending,
  handle,
  Listeners,
  type Senses,
} from './room.js'
import { hello, parseControl, played, sentMs } from './wire.js'

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
  const remote = new RemotePlayback(
    (utterance, ms) => {
      if (channel.readyState === 'open') channel.send(played(utterance, ms))
    },
    (audio) => room.hearing(audio)
  )
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
  const ending = new Ending(room, async () => {
    for (const stream of streams) for (const track of stream.getTracks()) track.stop()
    camera?.stop()
    remote.close()
    pc.close()
  })
  const ready = new Promise<void>((resolve, reject) => {
    channel.onopen = () => channel.send(hello())
    channel.onmessage = (message: MessageEvent) => {
      const frame = parseControl(String(message.data))
      if (frame.type === 'ready') resolve()
      else if (frame.type === 'error') reject(new SensesException(frame.message))
      else if (frame.type === 'speaking') remote.speaking(frame.utterance, sentMs(frame))
      else if (frame.type === 'silent') remote.silent(frame.utterance, sentMs(frame))
      else if (frame.type === 'stop') void remote.stop(frame.utterance, sentMs(frame))
      else if (frame.type === 'event') ending.tell(frame.event)
    }
  })
  ready.catch(() => {})
  channel.onclose = () => ending.lost()
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'failed')
      ending.lost('webrtc: the connection failed before it was ready')
    else if (pc.connectionState === 'closed') ending.lost()
  }
  const negotiate = async () => {
    if (options.microphone === false) pc.addTransceiver('audio', { direction: 'recvonly' })
    else {
      const microphone = await navigator.mediaDevices.getUserMedia({ audio: MICROPHONE })
      if (ending.stopped) {
        for (const track of microphone.getTracks()) track.stop()
        return
      }
      streams.push(microphone)
      for (const track of microphone.getAudioTracks()) pc.addTrack(track, microphone)
    }
    if (camera !== null && settings !== null) {
      await camera.use(settings.source)
      if (ending.stopped) return
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
      signal: options.signal ?? null,
    })
    if (!response.ok)
      throw new SensesException(`webrtc: the offer was refused with status ${response.status}`)
    const answer = (await response.json()) as { sdp: string }
    await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp })
    await ready
  }
  try {
    await ending.until(negotiate(), options.signal)
  } catch (error) {
    await ending.stop()
    throw error
  }
  const enable = (on: boolean) => {
    for (const track of microphone()) track.enabled = on
  }
  return handle(room, ending, camera, {
    mute: () => enable(false),
    unmute: () => enable(true),
    pauseCamera() {
      camera?.pause()
      if (room.status === 'open') void sender?.replaceTrack(null)
    },
    resumeCamera() {
      camera?.resume()
      if (room.status === 'open') void sender?.replaceTrack(camera?.track ?? null)
    },
    resumeAudio: () => remote.resume(),
    setFps(fps) {
      checkFps(fps, camera)
      camera?.setFps(fps)
      if (sender === null || room.status === 'closed') return
      const parameters = sender.getParameters()
      for (const encoding of parameters.encodings) encoding.maxFramerate = fps
      void sender.setParameters(parameters)
    },
  })
}
