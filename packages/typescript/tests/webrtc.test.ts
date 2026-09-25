import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SensesError } from '../src/errors'
import { connect } from '../src/index'
import { RemotePlayback } from '../src/remote-playback'
import { hello, played } from '../src/wire'
import { media, type Track } from './fakes'

class Channel {
  readyState = 'connecting'
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((message: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  constructor(readonly label: string) {}
  send(data: string): void {
    this.sent.push(data)
  }
  open(): void {
    this.readyState = 'open'
    this.onopen?.()
  }
  deliver(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
}

class Sender {
  replaced: Array<Track | null> = []
  parameters: { encodings: RTCRtpEncodingParameters[] } = { encodings: [] }
  async replaceTrack(track: Track | null): Promise<void> {
    this.replaced.push(track)
  }
  getParameters() {
    return structuredClone(this.parameters)
  }
  async setParameters(parameters: { encodings: RTCRtpEncodingParameters[] }): Promise<void> {
    this.parameters = parameters
  }
}

class Peer {
  static made: Peer[] = []
  iceGatheringState = 'complete'
  connectionState = 'new'
  localDescription: { sdp: string } | null = null
  remote: unknown = null
  channel: Channel | null = null
  added: Array<{ track: Track; init?: RTCRtpTransceiverInit }> = []
  sender = new Sender()
  transceivers: unknown[] = []
  closed = false
  ontrack: ((event: unknown) => void) | null = null
  onconnectionstatechange: (() => void) | null = null
  constructor(readonly config: RTCConfiguration) {
    Peer.made.push(this)
  }
  createDataChannel(label: string): Channel {
    this.channel = new Channel(label)
    return this.channel
  }
  addTrack(track: Track): void {
    this.added.push({ track })
  }
  addTransceiver(what: Track | string, init?: RTCRtpTransceiverInit): { sender: Sender } {
    if (typeof what === 'string') this.transceivers.push({ kind: what, init })
    else this.added.push({ track: what, ...(init ? { init } : {}) })
    this.sender.parameters.encodings = (init?.sendEncodings ?? []).map((encoding) => ({ ...encoding }))
    return { sender: this.sender }
  }
  async createOffer(): Promise<{ type: string; sdp: string }> {
    return { type: 'offer', sdp: 'v=0 offer' }
  }
  async setLocalDescription(offer: { sdp: string }): Promise<void> {
    this.localDescription = offer
  }
  async setRemoteDescription(answer: unknown): Promise<void> {
    this.remote = answer
  }
  addEventListener(): void {}
  close(): void {
    this.closed = true
  }
}

type Request = { url: string; init: RequestInit }

function server(status = 200) {
  const requests: Request[] = []
  const fetch = async (url: string, init: RequestInit) => {
    requests.push({ url, init })
    return { ok: status < 400, status, json: async () => ({ sdp: 'v=0 answer' }) }
  }
  return { requests, fetch }
}

async function connected(options: Record<string, unknown> = {}) {
  const connecting = connect({ url: '/senses/offer', transport: 'webrtc', ...options })
  await vi.waitFor(() => expect(Peer.made[0]?.remote).not.toBeNull())
  const peer = Peer.made[0] as Peer
  const channel = peer.channel as Channel
  channel.open()
  channel.deliver({ type: 'ready' })
  return { senses: await connecting, peer, channel }
}

beforeEach(() => {
  Peer.made = []
  vi.stubGlobal('RTCPeerConnection', Peer)
  vi.stubGlobal('navigator', media().navigator)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('connect over WebRTC', () => {
  it('posts the offer with the headers, says hello on the channel and waits for ready', async () => {
    const offers = server()
    vi.stubGlobal('fetch', offers.fetch)
    const { senses, peer, channel } = await connected({ headers: { authorization: 'Bearer t' } })
    const request = offers.requests[0] as Request
    expect(request.url).toBe('/senses/offer')
    expect(JSON.parse(String(request.init.body))).toEqual({ sdp: 'v=0 offer' })
    expect(request.init.headers).toEqual({ 'content-type': 'application/json', authorization: 'Bearer t' })
    expect(request.init.credentials).toBe('same-origin')
    expect(peer.remote).toEqual({ type: 'answer', sdp: 'v=0 answer' })
    expect(channel.sent).toEqual([hello()])
    expect(channel.label).toBe('senses')
    expect(senses.status).toBe('open')
  })

  it('rejects a refused offer and closes the peer', async () => {
    vi.stubGlobal('fetch', server(401).fetch)
    await expect(connect({ url: '/senses/offer', transport: 'webrtc' })).rejects.toEqual(
      new SensesError('webrtc: the offer was refused with status 401'),
    )
    expect((Peer.made[0] as Peer).closed).toBe(true)
  })

  it('caps the camera at its rate, pauses it, mutes the microphone and changes the rate', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const { senses, peer } = await connected({ camera: { fps: 3 } })
    const video = peer.added.find((a) => a.track.kind === 'video')
    const audio = peer.added.find((a) => a.track.kind === 'audio')
    expect(video?.init?.sendEncodings).toEqual([{ maxFramerate: 3 }])
    senses.pauseCamera()
    senses.mute()
    expect(video?.track.enabled).toBe(false)
    expect(audio?.track.enabled).toBe(false)
    senses.setFps(5)
    expect(video?.track.constraints).toEqual([{ frameRate: 5 }])
    expect(peer.sender.parameters.encodings).toEqual([{ maxFramerate: 5 }])
    await senses.close()
    expect(peer.closed).toBe(true)
  })

  it('replaces the camera track with the screen, keeps the cap, and puts the camera back when the share ends', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    const sources: unknown[] = []
    senses.subscribe((event) => event.kind === 'video' && sources.push(event.source))
    const camera = peer.added.find((a) => a.track.kind === 'video')?.track
    await senses.shareScreen()
    const screen = peer.sender.replaced[0]
    expect(screen?.source).toBe('screen')
    expect(camera?.stopped).toBe(true)
    senses.setFps(4)
    expect(screen?.constraints).toEqual([{ frameRate: 4 }])
    expect(peer.sender.parameters.encodings).toEqual([{ maxFramerate: 4 }])
    screen?.end()
    await vi.waitFor(() => expect(sources).toEqual(['screen', 'camera']))
    expect(peer.sender.replaced[1]?.source).toBe('camera')
    expect(peer.sender.replaced[1]?.stopped).toBe(false)
    expect(peer.closed).toBe(false)
  })

  it('starts on the screen when asked, and sends no video once the share ends', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const { senses, peer } = await connected({ camera: { source: 'screen', fps: 2 } })
    const sources: unknown[] = []
    senses.subscribe((event) => event.kind === 'video' && sources.push(event.source))
    const video = peer.added.find((a) => a.track.kind === 'video')
    expect(video?.track.source).toBe('screen')
    expect(video?.init?.sendEncodings).toEqual([{ maxFramerate: 2 }])
    video?.track.end()
    await vi.waitFor(() => expect(sources).toEqual([null]))
    expect(peer.sender.replaced).toEqual([null])
  })

  it('listens only, when there is no microphone, and refuses a rate without a camera', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const { senses, peer } = await connected({ microphone: false })
    expect(peer.transceivers).toEqual([{ kind: 'audio', init: { direction: 'recvonly' } }])
    expect(() => senses.setFps(2)).toThrow(new SensesError('senses: setFps needs a camera'))
  })
})

class Element {
  muted = false
  autoplay = false
  srcObject: unknown = null
  async play(): Promise<void> {}
  pause(): void {}
}

function receiver(counts: { emitted: number; concealed: number }) {
  return {
    getStats: async () =>
      new Map<string, Record<string, unknown>>([
        [
          'in',
          {
            type: 'inbound-rtp',
            jitterBufferEmittedCount: counts.emitted,
            concealedSamples: counts.concealed,
            codecId: 'c',
          },
        ],
        ['c', { id: 'c', clockRate: 48000 }],
      ]),
  } as unknown as RTCRtpReceiver
}

describe('remote playback', () => {
  it('counts what played from the receiver, per utterance', async () => {
    vi.stubGlobal('Audio', Element)
    const counts = { emitted: 4800, concealed: 0 }
    const reports: Array<[number, number]> = []
    const playback = new RemotePlayback((u, ms) => reports.push([u, ms]))
    playback.start({} as MediaStream, receiver(counts))
    playback.speaking(1)
    await Promise.resolve()
    counts.emitted += 9600
    counts.concealed += 480
    await playback.stop(1)
    expect(reports).toEqual([[1, 190]])
    const element = (playback as unknown as { element: Element }).element
    expect(element.muted).toBe(true)
    playback.speaking(2)
    expect(element.muted).toBe(false)
  })

  it('reports a natural end once the emitted count stops moving', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('Audio', Element)
    const counts = { emitted: 0, concealed: 0 }
    const reports: Array<[number, number]> = []
    const playback = new RemotePlayback((u, ms) => reports.push([u, ms]))
    playback.start({} as MediaStream, receiver(counts))
    playback.speaking(1)
    counts.emitted = 4800
    const ending = playback.silent(1)
    await vi.advanceTimersByTimeAsync(50)
    counts.emitted = 9600
    await vi.advanceTimersByTimeAsync(50)
    expect(reports).toEqual([])
    await vi.advanceTimersByTimeAsync(100)
    await ending
    expect(reports).toEqual([[1, 200]])
  })
})

describe('played', () => {
  it('is what the channel carries back', () => {
    expect(JSON.parse(played(1, 190))).toEqual({ type: 'played', utterance: 1, ms: 190 })
  })
})
