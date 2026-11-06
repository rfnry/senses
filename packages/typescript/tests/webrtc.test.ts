import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SensesException } from '../src/errors'
import { connect } from '../src/index'
import { RemotePlayback } from '../src/remote-playback'
import { hello, played } from '../src/wire'
import { behindAThrowingSubscriber, media, type Track, unhandledDuring } from './fakes'

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
  close(): void {
    this.readyState = 'closed'
    this.onclose?.()
  }
}

class Sender {
  replaced: Array<Track | null> = []
  parameters: { encodings: RTCRtpEncodingParameters[] } = { encodings: [] }
  closed = false
  replacing: Promise<void> | null = null
  async replaceTrack(track: Track | null): Promise<void> {
    if (this.replacing !== null) await this.replacing
    if (this.closed) throw new Error('InvalidStateError')
    this.replaced.push(track)
  }
  getParameters() {
    return structuredClone(this.parameters)
  }
  async setParameters(parameters: { encodings: RTCRtpEncodingParameters[] }): Promise<void> {
    if (this.closed) throw new Error('InvalidStateError')
    this.parameters = parameters
  }
}

class Peer {
  static made: Peer[] = []
  static answering: Promise<void> | null = null
  iceGatheringState = 'complete'
  connectionState = 'new'
  localDescription: { sdp: string } | null = null
  remote: unknown = null
  applying = false
  channel: Channel | null = null
  added: Array<{ track: Track; init?: RTCRtpTransceiverInit }> = []
  sender = new Sender()
  transceivers: unknown[] = []
  closed = false
  closes = 0
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
    this.sender.parameters.encodings = (init?.sendEncodings ?? []).map((encoding) => ({
      ...encoding,
    }))
    return { sender: this.sender }
  }
  async createOffer(): Promise<{ type: string; sdp: string }> {
    return { type: 'offer', sdp: 'v=0 offer' }
  }
  async setLocalDescription(offer: { sdp: string }): Promise<void> {
    this.localDescription = offer
  }
  async setRemoteDescription(answer: unknown): Promise<void> {
    this.applying = true
    if (Peer.answering !== null) await Peer.answering
    this.remote = answer
  }
  addEventListener(): void {}
  close(): void {
    this.closes += 1
    this.closed = true
    this.sender.closed = true
  }
  become(state: string): void {
    this.connectionState = state
    this.onconnectionstatechange?.()
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
  Peer.answering = null
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
    expect(request.init.headers).toEqual({
      'content-type': 'application/json',
      authorization: 'Bearer t',
    })
    expect(request.init.credentials).toBe('same-origin')
    expect(peer.remote).toEqual({ type: 'answer', sdp: 'v=0 answer' })
    expect(channel.sent).toEqual([hello()])
    expect(channel.label).toBe('senses')
    expect(senses.status).toBe('open')
  })

  it('rejects a refused offer, closes the peer and stops the microphone', async () => {
    vi.stubGlobal('fetch', server(401).fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    await expect(connect({ url: '/senses/offer', transport: 'webrtc' })).rejects.toEqual(
      new SensesException('webrtc: the offer was refused with status 401')
    )
    expect((Peer.made[0] as Peer).closed).toBe(true)
    expect(devices.streams[0]?.getAudioTracks()[0]?.stopped).toBe(true)
  })

  it('lets go of the peer and the microphone when the camera it was asked for is refused', async () => {
    const offers = server()
    vi.stubGlobal('fetch', offers.fetch)
    const devices = media()
    devices.refused.camera = true
    vi.stubGlobal('navigator', devices.navigator)
    await expect(
      connect({ url: '/senses/offer', transport: 'webrtc', camera: { deviceId: 'gone' } })
    ).rejects.toThrow('NotAllowedError')
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 640, frameRate: 1, deviceId: { exact: 'gone' } },
    })
    expect(offers.requests).toEqual([])
    expect((Peer.made[0] as Peer).closed).toBe(true)
    expect(devices.streams[0]?.getAudioTracks()[0]?.stopped).toBe(true)
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

  it('opens the camera asked for and flips it by replacing the track', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const { senses, peer } = await connected({ camera: { facing: 'environment', fps: 1 } })
    const sources: unknown[] = []
    senses.subscribe((event) => event.kind === 'video' && sources.push(event.source))
    expect(devices.asked.find((a) => a.source === 'camera')?.constraints).toEqual({
      video: { width: 640, frameRate: 1, facingMode: { ideal: 'environment' } },
    })
    const back = peer.added.find((a) => a.track.kind === 'video')?.track
    await senses.useCamera({ facing: 'user' })
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 640, frameRate: 1, facingMode: { ideal: 'user' } },
    })
    const front = peer.sender.replaced[0]
    expect(front).toBe(devices.video().at(-1))
    expect(front?.stopped).toBe(false)
    expect(back?.stopped).toBe(true)
    expect(peer.added.filter((a) => a.track.kind === 'video')).toHaveLength(1)
    expect(sources).toEqual(['camera'])
    await expect(senses.useCamera({ facing: 'user', deviceId: 'desk' })).rejects.toEqual(
      new SensesException('senses: the camera takes facing or deviceId, not both')
    )
    expect(peer.sender.replaced).toHaveLength(1)
  })

  it('refuses facing and deviceId together before making a peer', async () => {
    vi.stubGlobal('fetch', server().fetch)
    await expect(
      connect({
        url: '/senses/offer',
        transport: 'webrtc',
        camera: { facing: 'user', deviceId: 'desk' },
      })
    ).rejects.toEqual(new SensesException('senses: the camera takes facing or deviceId, not both'))
    expect(Peer.made).toHaveLength(0)
  })

  it('sends no track while the camera is paused, across a switch, and the track again on resume', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    senses.pauseCamera()
    expect(peer.sender.replaced).toEqual([null])
    await senses.shareScreen()
    expect(peer.sender.replaced).toEqual([null, null])
    const screen = devices.video()[1]
    expect(screen?.enabled).toBe(false)
    senses.resumeCamera()
    expect(peer.sender.replaced).toEqual([null, null, screen])
    expect(screen?.enabled).toBe(true)
  })

  it('leaves the closed peer and the camera alone when the camera is used after close', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const rejected: unknown[] = []
    const record = (reason: unknown) => rejected.push(reason)
    process.on('unhandledRejection', record)
    try {
      const { senses, peer } = await connected({ camera: { fps: 1 } })
      await senses.close()
      const asked = devices.asked.length
      senses.pauseCamera()
      senses.resumeCamera()
      senses.setFps(2)
      await senses.useCamera({ facing: 'user' })
      await senses.shareScreen()
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(rejected).toEqual([])
      expect(devices.asked).toHaveLength(asked)
      expect(devices.video().every((track) => track.stopped)).toBe(true)
      expect(peer.sender.replaced).toEqual([])
      expect(peer.sender.parameters.encodings).toEqual([{ maxFramerate: 1 }])
    } finally {
      process.off('unhandledRejection', record)
    }
  })

  it('keeps sending no track when the camera is switched while paused', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const { senses, peer } = await connected({ camera: { facing: 'environment', fps: 1 } })
    senses.pauseCamera()
    await senses.useCamera({ facing: 'user' })
    const front = devices.video().at(-1)
    expect(peer.sender.replaced).toEqual([null, null])
    expect(front?.enabled).toBe(false)
    senses.resumeCamera()
    expect(peer.sender.replaced).toEqual([null, null, front])
    expect(front?.enabled).toBe(true)
  })

  it('marks a screen track as text, so the encoder keeps its resolution, and leaves the camera alone', async () => {
    vi.stubGlobal('fetch', server().fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    const camera = peer.added.find((a) => a.track.kind === 'video')?.track
    await senses.shareScreen()
    expect(peer.sender.replaced[0]?.source).toBe('screen')
    expect(peer.sender.replaced[0]?.contentHint).toBe('text')
    expect(camera?.contentHint).toBe('')
    Peer.made = []
    const started = await connected({ camera: { source: 'screen', fps: 1 } })
    expect(started.peer.added.find((a) => a.track.kind === 'video')?.track.contentHint).toBe('text')
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
    expect(() => senses.setFps(2)).toThrow(new SensesException('senses: setFps needs a camera'))
  })
})

describe('when the connection ends over WebRTC', () => {
  const closed = { kind: 'closed', reason: 'closed', message: null, triggered_by: null }
  const event = { type: 'event', event: { scope: 'acme', at: 'now', ...closed } }

  function equipped() {
    vi.stubGlobal('fetch', server().fetch)
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    return devices
  }

  function expectReleased(devices: ReturnType<typeof media>, peer: Peer) {
    expect(devices.tracks().length).toBeGreaterThan(0)
    expect(devices.tracks().filter((track) => !track.stopped)).toEqual([])
    expect(peer.closed).toBe(true)
  }

  async function dialing(options: Record<string, unknown> = {}) {
    const connecting = connect({ url: '/senses/offer', transport: 'webrtc', ...options })
    connecting.catch(() => {})
    await vi.waitFor(() => expect(Peer.made[0]?.remote).not.toBeNull())
    const peer = Peer.made[0] as Peer
    return { connecting, peer, channel: peer.channel as Channel }
  }

  it('releases everything on the server closed event', async () => {
    const devices = equipped()
    const { senses, peer, channel } = await connected({ camera: { fps: 1 } })
    channel.deliver(event)
    expectReleased(devices, peer)
    expect(senses.status).toBe('closed')
  })

  it('releases everything when the data channel closes', async () => {
    const devices = equipped()
    const { senses, peer, channel } = await connected({ camera: { fps: 1 } })
    channel.close()
    expectReleased(devices, peer)
    expect(senses.status).toBe('closed')
  })

  it('releases everything when the peer fails after connecting', async () => {
    const devices = equipped()
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    peer.become('failed')
    expectReleased(devices, peer)
    expect(senses.status).toBe('closed')
  })

  it('stays connected through a peer that is only disconnected', async () => {
    const devices = equipped()
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    peer.become('disconnected')
    expect(senses.status).toBe('open')
    expect(peer.closed).toBe(false)
    expect(devices.tracks().filter((track) => track.stopped)).toEqual([])
  })

  it('rejects and releases everything when the peer fails before the data channel opens', async () => {
    const devices = equipped()
    const { connecting, peer } = await dialing()
    peer.become('failed')
    await expect(connecting).rejects.toEqual(
      new SensesException('webrtc: the connection failed before it was ready')
    )
    expectReleased(devices, peer)
  })

  it('rejects and releases everything when the peer closes before the data channel opens', async () => {
    const devices = equipped()
    const { connecting, peer } = await dialing()
    peer.become('closed')
    await expect(connecting).rejects.toEqual(
      new SensesException('transport: the connection closed before it was ready')
    )
    expectReleased(devices, peer)
  })

  it('rejects at once when the peer fails with its answer never applied', async () => {
    const devices = equipped()
    Peer.answering = new Promise<void>(() => {})
    const connecting = connect({ url: '/senses/offer', transport: 'webrtc' })
    connecting.catch(() => {})
    await vi.waitFor(() => expect(Peer.made[0]?.applying).toBe(true))
    const peer = Peer.made[0] as Peer
    peer.become('failed')
    await expect(connecting).rejects.toBeInstanceOf(SensesException)
    expectReleased(devices, peer)
  })

  it('rejects with the server message on an error before ready', async () => {
    const devices = equipped()
    const { connecting, peer, channel } = await dialing()
    channel.open()
    channel.deliver({ type: 'error', message: 'transport: protocol 1 is not supported' })
    await expect(connecting).rejects.toEqual(
      new SensesException('transport: protocol 1 is not supported')
    )
    expectReleased(devices, peer)
  })

  it('rejects on a closed event before ready, without waiting for the peer to fail', async () => {
    const devices = equipped()
    const { connecting, peer, channel } = await dialing()
    channel.open()
    channel.deliver(event)
    await expect(connecting).rejects.toEqual(
      new SensesException('transport: the connection closed before it was ready')
    )
    expectReleased(devices, peer)
  })

  it('resolves a second close, and a close after the far end', async () => {
    const devices = equipped()
    const first = await connected({ camera: { fps: 1 } })
    await first.senses.close()
    await expect(first.senses.close()).resolves.toBeUndefined()
    expectReleased(devices, first.peer)
    expect(first.peer.closes).toBe(1)

    Peer.made = []
    const second = await connected()
    second.channel.close()
    await expect(second.senses.close()).resolves.toBeUndefined()
    await expect(second.senses.close()).resolves.toBeUndefined()
    expect(second.peer.closes).toBe(1)
  })

  it('tells the closed event with the devices live, and the status once they are released', async () => {
    const devices = equipped()
    const { senses, channel } = await connected({ camera: { fps: 1 } })
    const live = () => devices.tracks().filter((track) => !track.stopped).length
    const seen: Array<[string, number]> = []
    senses.subscribe((frame) => {
      if (frame.kind === 'closed') seen.push(['closed event', live()])
      if (frame.kind === 'status') seen.push([frame.status, live()])
    })
    channel.deliver(event)
    expect(seen).toEqual([
      ['open', 2],
      ['closed event', 2],
      ['closed', 0],
    ])
  })

  it('tells every subscriber the closed event and the status when the first throws on both', async () => {
    vi.useFakeTimers()
    const devices = equipped()
    const { senses, peer, channel } = await connected({ camera: { fps: 1 } })
    const failed = { ...event.event, reason: 'failed', message: 'models: the connection dropped' }
    const told = behindAThrowingSubscriber(senses)
    const rejected = await unhandledDuring(async () => {
      channel.deliver({ type: 'event', event: failed })
      await senses.close()
    })
    expect(told).toEqual([
      { kind: 'status', status: 'open' },
      failed,
      { kind: 'status', status: 'closed' },
    ])
    expect(rejected).toEqual([])
    expect(() => vi.advanceTimersToNextTimer()).toThrow('a bug on closed')
    expect(() => vi.advanceTimersToNextTimer()).toThrow('a bug on status')
    expectReleased(devices, peer)
    expect(senses.status).toBe('closed')
  })

  it.each([
    ['the data channel closes', (_: Peer, channel: Channel) => channel.close()],
    ['the peer fails', (peer: Peer) => peer.become('failed')],
  ])('tells every subscriber the status when the first throws on it and %s', async (_, end) => {
    vi.useFakeTimers()
    const devices = equipped()
    const { senses, peer, channel } = await connected({ camera: { fps: 1 } })
    const told = behindAThrowingSubscriber(senses)
    const rejected = await unhandledDuring(async () => {
      end(peer, channel)
      await senses.close()
    })
    expect(told).toEqual([
      { kind: 'status', status: 'open' },
      { kind: 'status', status: 'closed' },
    ])
    expect(rejected).toEqual([])
    expect(() => vi.advanceTimersToNextTimer()).toThrow('a bug on status')
    expectReleased(devices, peer)
  })

  it('resolves close and tells every subscriber the status when the first throws on it', async () => {
    vi.useFakeTimers()
    const devices = equipped()
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    const told = behindAThrowingSubscriber(senses)
    const rejected = await unhandledDuring(async () => {
      await expect(senses.close()).resolves.toBeUndefined()
    })
    expect(told).toEqual([
      { kind: 'status', status: 'open' },
      { kind: 'status', status: 'closed' },
    ])
    expect(rejected).toEqual([])
    expect(() => vi.advanceTimersToNextTimer()).toThrow('a bug on status')
    expectReleased(devices, peer)
  })

  it('reopens no device after the far end, and touches no closed sender', async () => {
    const devices = equipped()
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    peer.become('failed')
    const asked = devices.asked.length
    await senses.useCamera({ facing: 'user' })
    await senses.shareScreen()
    senses.pauseCamera()
    senses.resumeCamera()
    senses.setFps(4)
    expect(devices.asked).toHaveLength(asked)
    expect(peer.sender.replaced).toEqual([])
    expect(peer.sender.parameters.encodings).toEqual([{ maxFramerate: 1 }])
  })

  it('lets go of the old camera when the connection ends during a switch that never settles', async () => {
    const devices = equipped()
    const { senses, peer } = await connected({ camera: { fps: 1 } })
    peer.sender.replacing = new Promise<void>(() => {})
    void senses.useCamera({ facing: 'user' })
    await vi.waitFor(() => expect(devices.video()).toHaveLength(2))
    peer.become('failed')
    expect(devices.video().filter((track) => !track.stopped)).toEqual([])
  })

  async function playing() {
    vi.useFakeTimers({ now: 0 })
    vi.stubGlobal('Audio', Element)
    const reader = receiver({ emitted: 0, packets: 0, concealed: 0 })
    const read = vi.spyOn(reader, 'getStats')
    const devices = equipped()
    const { peer, channel } = await connected()
    peer.ontrack?.({ streams: [{}], receiver: reader, track: {} })
    return { devices, peer, channel, read }
  }

  it('reads no stats and reports nothing once the connection has ended during a poll', async () => {
    const { devices, peer, channel, read } = await playing()
    channel.deliver({ type: 'speaking', utterance: 1 })
    channel.deliver({ type: 'silent', utterance: 1 })
    await vi.advanceTimersByTimeAsync(150)
    expect(vi.getTimerCount()).toBe(1)
    const sent = channel.sent.length
    const reads = read.mock.calls.length
    channel.readyState = 'closed'
    channel.deliver(event)
    expectReleased(devices, peer)
    await vi.advanceTimersByTimeAsync(100)
    expect(vi.getTimerCount()).toBe(0)
    expect(read.mock.calls).toHaveLength(reads)
    expect(channel.sent).toHaveLength(sent)
  })

  it('leaves no fade running once the connection has ended', async () => {
    const { channel } = await playing()
    channel.deliver({ type: 'speaking', utterance: 1 })
    channel.deliver({ type: 'stop', utterance: 1 })
    await vi.advanceTimersByTimeAsync(10)
    expect(vi.getTimerCount()).toBe(1)
    channel.deliver(event)
    expect(vi.getTimerCount()).toBe(0)
  })
})

class Element {
  muted = false
  volume = 1
  autoplay = false
  srcObject: unknown = null
  async play(): Promise<void> {}
  pause(): void {}
}

type Stats = {
  emitted: number
  packets: number
  concealed: number
  lost?: number
  discarded?: number
}

function receiver(counts: Stats) {
  return {
    getStats: async () =>
      new Map<string, Record<string, unknown>>([
        [
          'in',
          {
            type: 'inbound-rtp',
            jitterBufferEmittedCount: counts.emitted,
            packetsReceived: counts.packets,
            packetsLost: counts.lost ?? 0,
            packetsDiscarded: counts.discarded ?? 0,
            concealedSamples: counts.concealed,
            codecId: 'c',
          },
        ],
        ['c', { id: 'c', clockRate: 48000 }],
      ]),
  } as unknown as RTCRtpReceiver
}

type Stretch = {
  at: number
  ms: number
  starts?: number
  lost?: number
  discarded?: number
  unplayed?: number
}

function bufferedReceiver(audio: Stretch[]) {
  const counts = (t: number) => {
    let packets = 0
    let played = 0
    let lost = 0
    let discarded = 0
    let free = 0
    for (const stretch of audio) {
      const { at, ms, starts = 20, unplayed = 0 } = stretch
      const from = Math.max(at + starts, free)
      free = from + ms
      if (t <= at) continue
      const past = t > at + ms / 2
      const missing = past ? (stretch.lost ?? 0) : 0
      const dropped = past ? (stretch.discarded ?? 0) : 0
      packets += Math.min(ms / 20, Math.ceil((t - at + 120) / 20)) - missing + unplayed
      played += Math.floor(Math.min(Math.max(t - from, 0), ms - (missing + dropped) * 20) / 20) * 20
      lost += missing
      discarded += dropped
    }
    return { packets, played, lost, discarded }
  }
  return {
    getStats: async () => {
      const t = Date.now()
      const first = audio[0]?.at ?? 0
      if (t <= first) return new Map()
      const { packets, played, lost, discarded } = counts(t)
      return new Map<string, Record<string, unknown>>([
        [
          'in',
          {
            type: 'inbound-rtp',
            jitterBufferEmittedCount: played * 48,
            packetsReceived: packets,
            packetsLost: lost,
            packetsDiscarded: discarded,
            concealedSamples: (t - first - played) * 48,
            codecId: 'c',
          },
        ],
        ['c', { id: 'c', clockRate: 48000 }],
      ])
    },
  } as unknown as RTCRtpReceiver
}

async function play(
  audio: Stretch[],
  script: Array<[number, (playback: RemotePlayback) => unknown]>,
  until: number
) {
  vi.useFakeTimers({ now: 0 })
  vi.stubGlobal('Audio', Element)
  const reports = new Map<number, number>()
  const playback = new RemotePlayback((u, ms) => reports.set(u, ms))
  playback.start({} as MediaStream, bufferedReceiver(audio))
  let at = 0
  for (const [when, step] of script) {
    await vi.advanceTimersByTimeAsync(when - at)
    at = when
    void step(playback)
  }
  await vi.advanceTimersByTimeAsync(until - at)
  return reports
}

describe('remote playback', () => {
  it('counts what played from the receiver, per utterance', async () => {
    vi.stubGlobal('Audio', Element)
    const counts = { emitted: 0, packets: 0, concealed: 0 }
    const reports: Array<[number, number]> = []
    const playback = new RemotePlayback((u, ms) => reports.push([u, ms]))
    playback.start({} as MediaStream, receiver(counts))
    playback.speaking(1)
    await Promise.resolve()
    counts.emitted += 9600
    counts.packets += 10
    counts.concealed += 480
    await playback.stop(1)
    playback.speaking(2)
    await Promise.resolve()
    counts.emitted += 4800
    counts.packets += 5
    await playback.stop(2)
    expect(reports).toEqual([
      [1, 200],
      [2, 100],
    ])
  })

  it('fades the voice out over 60 ms on a stop, and brings it back for the next utterance', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('Audio', Element)
    const playback = new RemotePlayback(() => {})
    playback.start({} as MediaStream, receiver({ emitted: 0, packets: 0, concealed: 0 }))
    const element = (playback as unknown as { element: Element }).element
    playback.speaking(1)
    await playback.stop(1)
    expect([element.volume, element.muted]).toEqual([1, false])
    await vi.advanceTimersByTimeAsync(30)
    expect(element.volume).toBeCloseTo(0.5)
    expect(element.muted).toBe(false)
    await vi.advanceTimersByTimeAsync(30)
    expect([element.volume, element.muted]).toEqual([0, true])
    playback.speaking(2)
    expect([element.volume, element.muted]).toEqual([1, false])
    await playback.stop(2)
    await vi.advanceTimersByTimeAsync(20)
    playback.speaking(3)
    await vi.advanceTimersByTimeAsync(100)
    expect([element.volume, element.muted]).toEqual([1, false])
  })

  it('reports a natural end once the emitted count stops moving', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('Audio', Element)
    const counts = { emitted: 0, packets: 0, concealed: 0 }
    const reports: Array<[number, number]> = []
    const playback = new RemotePlayback((u, ms) => reports.push([u, ms]))
    playback.start({} as MediaStream, receiver(counts))
    playback.speaking(1)
    counts.emitted = 4800
    const ending = playback.silent(1)
    await vi.advanceTimersByTimeAsync(150)
    counts.emitted = 9600
    await vi.advanceTimersByTimeAsync(100)
    expect(reports).toEqual([])
    await vi.advanceTimersByTimeAsync(50)
    await ending
    expect(reports).toEqual([[1, 200]])
  })

  it('the first utterance reports what played before any packet arrived', async () => {
    const reports = await play(
      [{ at: 0, ms: 1800 }],
      [
        [0, (p) => p.speaking(1)],
        [1660, (p) => p.silent(1)],
      ],
      4000
    )
    expect(Math.abs((reports.get(1) ?? 0) - 1800)).toBeLessThanOrEqual(20)
  })

  it('the first utterance still counts when its audio starts after its silent', async () => {
    const reports = await play(
      [{ at: 150, ms: 100 }],
      [
        [0, (p) => p.speaking(1)],
        [1, (p) => p.silent(1)],
      ],
      2000
    )
    expect(reports.get(1)).toBe(100)
  })

  it('a natural end reports the whole utterance', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5000, ms: 1800 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.speaking(2)],
        [6660, (p) => p.silent(2)],
      ],
      9000
    )
    expect(reports.get(2)).toBeGreaterThanOrEqual(1800 - 20)
    expect(reports.get(2)).toBe(1800)
  })

  it('a natural end still reports the utterance when its audio starts late', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5150, ms: 100 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.speaking(2)],
        [5001, (p) => p.silent(2)],
      ],
      9000
    )
    expect(reports.get(2)).toBe(100)
  })

  it('a short reply whose audio starts late reports its length, and the next reply its own', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5000, ms: 400, starts: 850 },
        { at: 7000, ms: 1800 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.speaking(2)],
        [5260, (p) => p.silent(2)],
        [7000, (p) => p.speaking(3)],
        [8660, (p) => p.silent(3)],
      ],
      10000
    )
    expect(reports.get(2)).toBe(400)
    expect(reports.get(3)).toBe(1800)
  })

  it('reports at once at a count that does not divide evenly into milliseconds', async () => {
    const reports = await play(
      [{ at: 0, ms: 4020 }],
      [
        [0, (p) => p.speaking(1)],
        [3880, (p) => p.silent(1)],
      ],
      4500
    )
    expect(reports.get(1)).toBe(4020)
  })

  it('a report does not wait for ever on packets that arrived and never play', async () => {
    const reports = await play(
      [{ at: 0, ms: 300, unplayed: 3 }],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
      ],
      1900
    )
    expect(reports.has(1)).toBe(false)
    await vi.advanceTimersByTimeAsync(500)
    expect(reports.get(1)).toBe(300)
  })

  it('a played utterance with lost packets reads whole', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5000, ms: 1800, lost: 2 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.speaking(2)],
        [6660, (p) => p.silent(2)],
      ],
      9000
    )
    expect(reports.get(2)).toBe(1800)
  })

  it('a lost packet does not move the edge between back-to-back responses', async () => {
    const reports = await play(
      [{ at: 0, ms: 6000, lost: 2 }],
      [
        [0, (p) => p.speaking(1)],
        [3860, (p) => p.silent(1)],
        [3880, (p) => p.speaking(2)],
        [5860, (p) => p.silent(2)],
      ],
      8000
    )
    expect(reports.get(1)).toBe(4000)
    expect(reports.get(2)).toBeGreaterThanOrEqual(2000)
  })

  it('a played utterance with packets too late to play reads whole', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5000, ms: 1800, discarded: 2 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.speaking(2)],
        [6660, (p) => p.silent(2)],
      ],
      9000
    )
    expect(reports.get(2)).toBe(1800)
  })

  it('a packet that turns from lost to too late never moves the count back', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('Audio', Element)
    const counts: Stats = { emitted: 0, packets: 0, concealed: 0 }
    const reports: Array<[number, number]> = []
    const playback = new RemotePlayback((u, ms) => reports.push([u, ms]))
    playback.start({} as MediaStream, receiver(counts))
    playback.speaking(1)
    counts.emitted = 9600
    counts.packets = 10
    counts.lost = 1
    const ending = playback.silent(1)
    await vi.advanceTimersByTimeAsync(100)
    counts.packets = 11
    counts.lost = 0
    counts.discarded = 1
    await vi.advanceTimersByTimeAsync(100)
    await ending
    expect(reports).toEqual([[1, 220]])
  })

  it('a negative packetsLost, from duplicates, adds nothing', async () => {
    vi.stubGlobal('Audio', Element)
    const counts: Stats = { emitted: 0, packets: 0, concealed: 0 }
    const reports: Array<[number, number]> = []
    const playback = new RemotePlayback((u, ms) => reports.push([u, ms]))
    playback.start({} as MediaStream, receiver(counts))
    playback.speaking(1)
    await Promise.resolve()
    counts.emitted = 9600
    counts.packets = 11
    counts.lost = -1
    await playback.stop(1)
    expect(reports).toEqual([[1, 200]])
  })

  it('a cut in the second of two responses reports what played of each', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5000, ms: 4000 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.speaking(2)],
        [6860, (p) => p.silent(2)],
        [6880, (p) => p.speaking(3)],
        [8020, (p) => p.stop(3)],
      ],
      9000
    )
    expect(reports.get(2)).toBe(2000)
    expect(Math.abs((reports.get(3) ?? 0) - 1000)).toBeLessThanOrEqual(40)
  })

  it('back-to-back responses each report all of their own audio', async () => {
    const reports = await play(
      [{ at: 0, ms: 4000 }],
      [
        [0, (p) => p.speaking(1)],
        [1860, (p) => p.silent(1)],
        [1880, (p) => p.speaking(2)],
        [3860, (p) => p.silent(2)],
      ],
      6000
    )
    expect(reports.get(1)).toBe(2000)
    expect(reports.get(2)).toBeGreaterThanOrEqual(2000)
    expect(reports.get(2)).toBeLessThanOrEqual(2020)
  })

  it('a natural end that played nothing reports nothing', async () => {
    const reports = await play(
      [{ at: 0, ms: 2000 }],
      [
        [0, (p) => p.speaking(1)],
        [1860, (p) => p.silent(1)],
        [1880, (p) => p.speaking(2)],
        [1890, (p) => p.silent(2)],
      ],
      4000
    )
    expect(reports.get(2)).toBe(0)
  })

  it('a response reports once it has played, without waiting for the next one', async () => {
    const reports = await play(
      [{ at: 0, ms: 4000 }],
      [
        [0, (p) => p.speaking(1)],
        [1860, (p) => p.silent(1)],
        [1880, (p) => p.speaking(2)],
      ],
      2200
    )
    expect([...reports]).toEqual([[1, 2000]])
  })

  it('back-to-back responses read whole when a packet beats its speaking', async () => {
    const reports = await play(
      [{ at: 0, ms: 4000 }],
      [
        [0, (p) => p.speaking(1)],
        [1860, (p) => p.silent(1)],
        [1881, (p) => p.speaking(2)],
        [3860, (p) => p.silent(2)],
      ],
      6000
    )
    expect(reports.get(1)).toBeGreaterThanOrEqual(2000)
    expect(reports.get(2)).toBeGreaterThanOrEqual(2000)
  })

  it('a speaking handled late still reports the whole utterance', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5000, ms: 1800 },
      ],
      [
        [40, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5150, (p) => p.speaking(2)],
        [6660, (p) => p.silent(2)],
      ],
      9000
    )
    expect(reports.get(1)).toBe(300)
    expect(reports.get(2)).toBe(1800)
  })

  it('the utterance after a cut does not count the audio the cut left playing', async () => {
    const reports = await play(
      [
        { at: 0, ms: 1100 },
        { at: 1020, ms: 1000 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [1000, (p) => p.stop(1)],
        [1020, (p) => p.speaking(2)],
        [1500, (p) => p.stop(2)],
      ],
      3000
    )
    expect(reports.get(1)).toBe(980)
    expect(Math.abs((reports.get(2) ?? 0) - 380)).toBeLessThanOrEqual(40)
  })

  it('the fade of an utterance that never reached the client is not counted as the next one', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300 },
        { at: 5000, ms: 60 },
        { at: 6000, ms: 1800 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.stop(2)],
        [6000, (p) => p.speaking(3)],
        [7000, (p) => p.stop(3)],
      ],
      8000
    )
    expect(Math.abs((reports.get(3) ?? 0) - 980)).toBeLessThanOrEqual(40)
  })

  it('a silent that comes after the next speaking still ends at the next utterance', async () => {
    const reports = await play(
      [{ at: 0, ms: 4000 }],
      [
        [0, (p) => p.speaking(1)],
        [1880, (p) => p.speaking(2)],
        [2500, (p) => p.silent(1)],
        [3860, (p) => p.silent(2)],
      ],
      6000
    )
    expect(reports.get(1)).toBe(2000)
    expect(reports.get(2)).toBeGreaterThanOrEqual(2000)
  })

  it('a cut while the previous response is still playing out counts neither past it', async () => {
    const reports = await play(
      [{ at: 0, ms: 4000 }],
      [
        [0, (p) => p.speaking(1)],
        [1860, (p) => p.silent(1)],
        [1880, (p) => p.speaking(2)],
        [1920, (p) => p.stop(2)],
      ],
      3000
    )
    expect(reports.get(1)).toBe(1900)
    expect(reports.get(2)).toBe(0)
  })

  it('packets the jitter buffer never played do not shift the next utterance', async () => {
    const reports = await play(
      [
        { at: 0, ms: 300, unplayed: 3 },
        { at: 5000, ms: 4000 },
      ],
      [
        [0, (p) => p.speaking(1)],
        [160, (p) => p.silent(1)],
        [5000, (p) => p.speaking(2)],
        [6860, (p) => p.silent(2)],
        [6880, (p) => p.speaking(3)],
        [8860, (p) => p.silent(3)],
      ],
      11000
    )
    expect(reports.get(1)).toBe(300)
    expect(reports.get(2)).toBe(2000)
    expect(reports.get(3)).toBeGreaterThanOrEqual(2000)
    expect(reports.get(3)).toBeLessThanOrEqual(2020)
  })

  it('ignores a speaking at or below the last stop', async () => {
    const reports = await play(
      [{ at: 0, ms: 1000 }],
      [
        [0, (p) => p.speaking(1)],
        [500, (p) => p.stop(3)],
        [510, (p) => p.speaking(2)],
        [520, (p) => p.silent(2)],
      ],
      2000
    )
    expect([...reports].sort()).toEqual([
      [1, 480],
      [3, 0],
    ])
  })
})

describe('played', () => {
  it('is what the channel carries back', () => {
    expect(JSON.parse(played(1, 190))).toEqual({ type: 'played', utterance: 1, ms: 190 })
  })
})
