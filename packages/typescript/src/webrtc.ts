import { SensesError } from './errors.js'
import { MICROPHONE } from './microphone.js'
import { RemotePlayback } from './remote-playback.js'
import { type ConnectOptions, cameraOptions, checkFps, Listeners, type Senses } from './room.js'
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
  const room = new Listeners()
  const pc = new RTCPeerConnection({ iceServers: options.iceServers ?? [] })
  const channel = pc.createDataChannel('senses', { ordered: true })
  const remote = new RemotePlayback((utterance, ms) => {
    if (channel.readyState === 'open') channel.send(played(utterance, ms))
  })
  const streams: MediaStream[] = []
  const tracks = (kind: 'audio' | 'video') =>
    streams.flatMap((stream) => stream.getTracks()).filter((t) => t.kind === kind)
  const camera = options.camera ? cameraOptions(options.camera) : null
  const ready = new Promise<void>((resolve, reject) => {
    let open = false
    channel.onopen = () => channel.send(hello())
    channel.onmessage = (message: MessageEvent) => {
      const frame = parseControl(String(message.data))
      if (!open) {
        if (frame.type === 'ready') {
          open = true
          resolve()
        } else if (frame.type === 'error') reject(new SensesError(frame.message))
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
      if (!open) reject(new SensesError('transport: the connection closed before it was ready'))
      room.end()
    }
  })
  ready.catch(() => {})
  const stop = () => {
    for (const stream of streams) for (const track of stream.getTracks()) track.stop()
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
    if (camera !== null) {
      const video = await navigator.mediaDevices.getUserMedia({ video: { width: camera.width, frameRate: camera.fps } })
      streams.push(video)
      for (const track of video.getVideoTracks()) {
        pc.addTransceiver(track, {
          direction: 'sendonly',
          streams: [video],
          sendEncodings: [{ maxFramerate: camera.fps }],
        })
      }
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
    if (!response.ok) throw new SensesError(`webrtc: the offer was refused with status ${response.status}`)
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
  const enable = (kind: 'audio' | 'video', on: boolean) => {
    for (const track of tracks(kind)) track.enabled = on
  }
  return {
    get status() {
      return room.status
    },
    subscribe: (listener) => room.subscribe(listener),
    mute: () => enable('audio', false),
    unmute: () => enable('audio', true),
    pauseCamera: () => enable('video', false),
    resumeCamera: () => enable('video', true),
    setFps(fps) {
      checkFps(fps, camera)
      for (const track of tracks('video')) void track.applyConstraints({ frameRate: fps })
    },
    async close() {
      stop()
    },
  }
}
