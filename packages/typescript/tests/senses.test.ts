import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connect, type Senses, SensesException, type SensesListener } from '../src/index'
import { encodeAudio, encodeImage, hello, played } from '../src/wire'
import {
  behindAThrowingSubscriber,
  Context,
  documentWithCanvas,
  JPEG,
  media,
  Socket,
  unhandledDuring,
  WorkletNode,
} from './fakes'

const ANA = { role: 'user', id: 'ana', name: 'Ana', metadata: {} }
const at = { scope: 'acme', at: '2026-09-24T12:03:04.200000Z' }

function audio(utterance: number, ms = 100): ArrayBuffer {
  const frame = encodeAudio(new Uint8Array(32 * ms), 16000)
  new DataView(frame.buffer).setUint32(6, utterance, true)
  return frame.buffer.slice(frame.byteOffset, frame.byteOffset + frame.byteLength) as ArrayBuffer
}

async function opened(
  options: Partial<Parameters<typeof connect>[0]> = {}
): Promise<{ senses: Senses; socket: Socket }> {
  const connecting = connect({
    url: 'wss://example.com/senses',
    transport: 'websocket',
    microphone: false,
    ...options,
  })
  await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
  const socket = Socket.made[0] as Socket
  socket.open()
  socket.deliver({ type: 'ready' })
  return { senses: await connecting, socket }
}

beforeEach(() => {
  Socket.made = []
  Context.made = []
  WorkletNode.made = []
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('AudioContext', Context)
  vi.stubGlobal('AudioWorkletNode', WorkletNode)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('connect over a WebSocket', () => {
  it('opens with the subprotocols, says hello and waits for ready', async () => {
    let done = false
    const connecting = connect({
      url: 'wss://example.com/senses',
      transport: 'websocket',
      protocols: ['bearer', 't'],
      microphone: false,
    }).then((senses) => {
      done = true
      return senses
    })
    await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
    const socket = Socket.made[0] as Socket
    expect(socket.protocols).toEqual(['bearer', 't'])
    expect(socket.binaryType).toBe('arraybuffer')
    socket.open()
    expect(socket.sent).toEqual([hello()])
    await Promise.resolve()
    expect(done).toBe(false)
    socket.deliver({ type: 'ready' })
    expect((await connecting).status).toBe('open')
  })

  it('rejects with the server error before ready', async () => {
    const connecting = connect({
      url: 'wss://example.com/senses',
      transport: 'websocket',
      microphone: false,
    })
    await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
    const socket = Socket.made[0] as Socket
    socket.open()
    socket.deliver({
      type: 'error',
      message: 'transport: protocol 1 is not supported; this server speaks 2',
    })
    await expect(connecting).rejects.toEqual(
      new SensesException('transport: protocol 1 is not supported; this server speaks 2')
    )
    expect((Context.made[0] as Context).closed).toBe(true)
  })

  it('lets go of the socket, the microphone and the audio when the camera is refused', async () => {
    const devices = media()
    devices.refused.camera = true
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const connecting = connect({
      url: 'wss://example.com/senses',
      transport: 'websocket',
      camera: { deviceId: 'gone' },
    })
    await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
    const socket = Socket.made[0] as Socket
    socket.open()
    socket.deliver({ type: 'ready' })
    await expect(connecting).rejects.toThrow('NotAllowedError')
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 640, frameRate: 1, deviceId: { exact: 'gone' } },
    })
    const worklet = WorkletNode.made[0] as WorkletNode
    worklet.port.post(new Uint8Array(640))
    expect(socket.binary()).toEqual([])
    expect(devices.streams[0]?.getAudioTracks()[0]?.stopped).toBe(true)
    expect(socket.readyState).toBe(3)
    expect(Context.made.map((context) => context.closed)).toEqual([true, true])
  })

  it('tells subscribers the status, then every event, until they leave', async () => {
    const { senses, socket } = await opened()
    const seen: Parameters<SensesListener>[0][] = []
    const leave = senses.subscribe((event) => seen.push(event))
    const heard = { ...at, kind: 'heard', text: 'hi', final: true, speaker: ANA }
    socket.deliver({ type: 'event', event: heard })
    leave()
    socket.deliver({ type: 'event', event: { ...at, kind: 'speaking' } })
    expect(seen).toEqual([{ kind: 'status', status: 'open' }, heard])
  })

  it('plays the voice and reports what played', async () => {
    const { socket } = await opened()
    const context = Context.made[0] as Context
    socket.deliver({ type: 'speaking', utterance: 1 })
    socket.deliver(audio(1))
    socket.deliver(audio(1))
    expect(context.nodes).toHaveLength(2)
    socket.deliver({ type: 'silent', utterance: 1 })
    for (const node of context.nodes) node.onended?.()
    expect(socket.sent.at(-1)).toBe(played(1, 200))
    socket.deliver({ type: 'speaking', utterance: 2 })
    socket.deliver(audio(2))
    socket.deliver({ type: 'stop', utterance: 2 })
    expect(socket.sent.at(-1)).toBe(played(2, 0))
    socket.deliver(audio(2))
    expect(context.nodes).toHaveLength(3)
  })

  it('sends the microphone until muted, and again once unmuted', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const { senses, socket } = await opened({ microphone: true })
    const port = (WorkletNode.made[0] as WorkletNode).port
    const pcm = new Uint8Array(640).fill(1)
    port.post(pcm)
    senses.mute()
    port.post(pcm)
    expect(devices.streams[0]?.getAudioTracks()[0]?.enabled).toBe(false)
    senses.unmute()
    port.post(pcm)
    expect(socket.binary()).toEqual([encodeAudio(pcm, 16000), encodeAudio(pcm, 16000)])
  })

  it('sends camera frames at the rate asked, and changes it', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('navigator', media().navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const { senses, socket } = await opened({ camera: { fps: 2 } })
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toEqual([encodeImage(JPEG), encodeImage(JPEG)])
    senses.setFps(4)
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(6)
    senses.pauseCamera()
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(6)
    expect(() => senses.setFps(0)).toThrow(new SensesException('senses: fps must be positive'))
  })

  it('keeps the camera stopped when the rate changes after close', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('navigator', media().navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const { senses } = await opened({ camera: { fps: 2 } })
    await senses.close()
    senses.setFps(4)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shares the screen, keeps sending frames, and returns to the camera when the share ends', async () => {
    vi.useFakeTimers()
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const { senses, socket } = await opened({ camera: { fps: 2, width: 1280 } })
    const sources: unknown[] = []
    senses.subscribe((event) => event.kind === 'video' && sources.push(event.source))
    const [camera] = devices.video()
    await senses.shareScreen()
    const screen = devices.video()[1]
    expect(screen?.source).toBe('screen')
    expect(camera?.stopped).toBe(true)
    expect(devices.asked.at(-1)).toEqual({
      source: 'screen',
      constraints: { video: { width: 1280, frameRate: 2 }, audio: false },
    })
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(2)
    senses.setFps(4)
    expect(screen?.constraints).toEqual([{ frameRate: 4 }])
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(6)
    screen?.end()
    await vi.waitFor(() => expect(sources).toEqual(['screen', 'camera']))
    expect(devices.video()[2]?.source).toBe('camera')
    expect(devices.asked.at(-1)?.constraints).toEqual({ video: { width: 1280, frameRate: 4 } })
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(10)
  })

  it('stops sending when a share it started with ends, until the camera is asked for', async () => {
    vi.useFakeTimers()
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const { senses, socket } = await opened({ camera: { source: 'screen', fps: 1 } })
    const sources: unknown[] = []
    senses.subscribe((event) => event.kind === 'video' && sources.push(event.source))
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(1)
    devices.video()[0]?.end()
    await vi.waitFor(() => expect(sources).toEqual([null]))
    await vi.advanceTimersByTimeAsync(3000)
    expect(socket.binary()).toHaveLength(1)
    await senses.useCamera()
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(2)
    expect(sources).toEqual([null, 'camera'])
  })

  it('keeps the camera when the person cancels the screen picker, and stays paused across a switch', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const { senses } = await opened({ camera: {} })
    devices.refused.screen = true
    await expect(senses.shareScreen()).rejects.toThrow('NotAllowedError')
    expect(devices.video()[0]?.stopped).toBe(false)
    devices.refused.screen = false
    senses.pauseCamera()
    await senses.shareScreen()
    expect(devices.video()[1]?.enabled).toBe(false)
  })

  it('opens the camera asked for, flips it in place, and returns to the last one after a share', async () => {
    vi.useFakeTimers()
    const devices = media()
    const page = documentWithCanvas()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', page)
    const { senses, socket } = await opened({
      camera: { facing: 'environment', width: 1280, fps: 1 },
    })
    const sources: unknown[] = []
    senses.subscribe((event) => event.kind === 'video' && sources.push(event.source))
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 1280, frameRate: 1, facingMode: { ideal: 'environment' } },
    })
    const [back] = devices.video()
    await senses.useCamera({ facing: 'user' })
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 1280, frameRate: 1, facingMode: { ideal: 'user' } },
    })
    expect(back?.stopped).toBe(true)
    expect((page.createElement('video') as { srcObject: unknown }).srcObject).toBe(
      devices.streams.at(-1)
    )
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(1)
    await senses.useCamera({ deviceId: 'desk' })
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 1280, frameRate: 1, deviceId: { exact: 'desk' } },
    })
    await senses.shareScreen()
    devices.video().at(-1)?.end()
    await vi.waitFor(() => expect(sources).toEqual(['camera', 'camera', 'screen', 'camera']))
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 1280, frameRate: 1, deviceId: { exact: 'desk' } },
    })
    await senses.useCamera()
    expect(devices.asked.at(-1)?.constraints).toEqual({
      video: { width: 1280, frameRate: 1, deviceId: { exact: 'desk' } },
    })
  })

  it('refuses facing and deviceId together, before opening anything', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const both = new SensesException('senses: the camera takes facing or deviceId, not both')
    await expect(
      connect({
        url: 'wss://example.com/senses',
        transport: 'websocket',
        camera: { facing: 'environment', deviceId: 'desk' },
      })
    ).rejects.toEqual(both)
    expect(Socket.made).toHaveLength(0)
    expect(devices.asked).toHaveLength(0)
    const { senses } = await opened({ camera: {} })
    await expect(senses.useCamera({ facing: 'user', deviceId: 'desk' })).rejects.toEqual(both)
    expect(devices.video()).toHaveLength(1)
    expect(devices.video()[0]?.stopped).toBe(false)
  })

  it('sends no frames from a camera switched to while paused, until resumed', async () => {
    vi.useFakeTimers()
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const { senses, socket } = await opened({ camera: { fps: 1 } })
    senses.pauseCamera()
    await senses.useCamera({ facing: 'user' })
    await vi.advanceTimersByTimeAsync(3000)
    expect(socket.binary()).toHaveLength(0)
    expect(devices.video().at(-1)?.enabled).toBe(false)
    senses.resumeCamera()
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket.binary()).toHaveLength(1)
  })

  it('never opens the camera again after close', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const { senses } = await opened({ camera: {} })
    await senses.close()
    const asked = devices.asked.length
    await senses.useCamera({ facing: 'user' })
    await senses.shareScreen()
    expect(devices.asked).toHaveLength(asked)
    expect(devices.video().every((track) => track.stopped)).toBe(true)
  })

  it('refuses a screen without the camera option', async () => {
    const { senses } = await opened()
    await expect(senses.shareScreen()).rejects.toEqual(
      new SensesException('senses: shareScreen needs the camera option')
    )
    await expect(senses.useCamera()).rejects.toEqual(
      new SensesException('senses: useCamera needs the camera option')
    )
  })

  it('refuses a rate without a camera', async () => {
    const { senses } = await opened()
    expect(() => senses.setFps(2)).toThrow(new SensesException('senses: setFps needs a camera'))
  })

  it('closes on the server closed event, on the socket closing, and on close()', async () => {
    const first = await opened()
    const statuses: string[] = []
    first.senses.subscribe((event) => event.kind === 'status' && statuses.push(event.status))
    first.socket.deliver({
      type: 'event',
      event: { ...at, kind: 'closed', reason: 'hung_up', message: null },
    })
    expect(first.senses.status).toBe('closed')
    expect(statuses).toEqual(['open', 'closed'])

    Socket.made = []
    const second = await opened()
    second.socket.close()
    expect(second.senses.status).toBe('closed')

    Socket.made = []
    Context.made = []
    const third = await opened()
    await third.senses.close()
    expect(third.socket.readyState).toBe(3)
    expect((Context.made[0] as Context).closed).toBe(true)
    expect(third.senses.status).toBe('closed')
  })
})

describe('when the connection ends over a WebSocket', () => {
  const closed = { ...at, kind: 'closed', reason: 'closed', message: null, triggered_by: null }
  const early = new SensesException('transport: the connection closed before it was ready')

  function equipped() {
    vi.useFakeTimers()
    const devices = media()
    const page = documentWithCanvas()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', page)
    return { devices, page }
  }

  function expectReleased(devices: ReturnType<typeof media>, socket: Socket) {
    expect(devices.tracks().length).toBeGreaterThan(0)
    expect(devices.tracks().filter((track) => !track.stopped)).toEqual([])
    expect(Context.made.length).toBeGreaterThan(0)
    expect(Context.made.filter((context) => !context.closed)).toEqual([])
    expect(socket.readyState).toBe(3)
    expect(vi.getTimerCount()).toBe(0)
  }

  async function dialing(options: Partial<Parameters<typeof connect>[0]> = {}) {
    const connecting = connect({
      url: 'wss://example.com/senses',
      transport: 'websocket',
      ...options,
    })
    connecting.catch(() => {})
    await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
    const socket = Socket.made[0] as Socket
    socket.open()
    return { connecting, socket }
  }

  function promptHeld(devices: ReturnType<typeof media>) {
    let answer = () => {}
    let refuse = () => {}
    devices.prompt.answered = new Promise<void>((resolve, reject) => {
      answer = resolve
      refuse = () => reject(new Error('NotAllowedError'))
    })
    return { answer: () => answer(), refuse: () => refuse() }
  }

  it('releases everything on the server closed event', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
    socket.deliver({ type: 'event', event: closed })
    expectReleased(devices, socket)
    expect(senses.status).toBe('closed')
  })

  it('releases everything when the socket closes', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
    socket.close()
    expectReleased(devices, socket)
    expect(senses.status).toBe('closed')
  })

  it('rejects at once and lets go of the microphone when the socket closes during its prompt', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const held = promptHeld(devices)
    const { connecting, socket } = await dialing()
    socket.deliver({ type: 'ready' })
    await vi.waitFor(() => expect(devices.asked).toHaveLength(1))
    socket.close()
    await expect(connecting).rejects.toEqual(early)
    expect(Context.made.map((context) => context.closed)).toEqual([true])
    held.answer()
    await vi.waitFor(() => expect(devices.tracks()).toHaveLength(1))
    await vi.waitFor(() => expect(devices.tracks()[0]?.stopped).toBe(true))
    expect(Context.made).toHaveLength(1)
    expect(WorkletNode.made).toHaveLength(0)
  })

  it('rejects when the server closes the session during the prompt', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    promptHeld(devices)
    const { connecting, socket } = await dialing()
    socket.deliver({ type: 'ready' })
    await vi.waitFor(() => expect(devices.asked).toHaveLength(1))
    socket.deliver({ type: 'event', event: closed })
    await expect(connecting).rejects.toEqual(early)
    expect(socket.readyState).toBe(3)
    expect(Context.made.map((context) => context.closed)).toEqual([true])
  })

  it('raises nothing more when the person refuses a prompt the connection outlived', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    const held = promptHeld(devices)
    const rejected: unknown[] = []
    const record = (reason: unknown) => rejected.push(reason)
    process.on('unhandledRejection', record)
    try {
      const { connecting, socket } = await dialing()
      socket.deliver({ type: 'ready' })
      await vi.waitFor(() => expect(devices.asked).toHaveLength(1))
      socket.close()
      await expect(connecting).rejects.toEqual(early)
      held.refuse()
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(rejected).toEqual([])
    } finally {
      process.off('unhandledRejection', record)
    }
  })

  it('rejects and lets go of the camera it was still asking for', async () => {
    const devices = media()
    vi.stubGlobal('navigator', devices.navigator)
    vi.stubGlobal('document', documentWithCanvas())
    const held = promptHeld(devices)
    const { connecting, socket } = await dialing({ microphone: false, camera: {} })
    socket.deliver({ type: 'ready' })
    await vi.waitFor(() => expect(devices.asked).toHaveLength(1))
    socket.close()
    await expect(connecting).rejects.toEqual(early)
    held.answer()
    await vi.waitFor(() => expect(devices.video()).toHaveLength(1))
    await vi.waitFor(() => expect(devices.video()[0]?.stopped).toBe(true))
    expect(Context.made.filter((context) => !context.closed)).toEqual([])
  })

  it('rejects with the reason of a session that failed before ready', async () => {
    const { connecting, socket } = await dialing({ microphone: false })
    const failed = { ...closed, reason: 'failed', message: 'session: the live model failed' }
    socket.deliver({ type: 'event', event: failed })
    await expect(connecting).rejects.toEqual(new SensesException('session: the live model failed'))
  })

  describe('with a signal', () => {
    const aborted = new SensesException('senses: connect was aborted')

    it('rejects at once and takes nothing when the signal has already aborted', async () => {
      const devices = media()
      vi.stubGlobal('navigator', devices.navigator)
      await expect(
        connect({
          url: 'wss://example.com/senses',
          transport: 'websocket',
          camera: {},
          signal: AbortSignal.abort(),
        })
      ).rejects.toEqual(aborted)
      expect(Socket.made).toEqual([])
      expect(Context.made).toEqual([])
      expect(devices.asked).toEqual([])
    })

    it('rejects and closes the socket when aborted before it opens', async () => {
      const controller = new AbortController()
      const connecting = connect({
        url: 'wss://example.com/senses',
        transport: 'websocket',
        microphone: false,
        signal: controller.signal,
      })
      connecting.catch(() => {})
      await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
      controller.abort()
      await expect(connecting).rejects.toEqual(aborted)
      expect((Socket.made[0] as Socket).readyState).toBe(3)
      expect(Context.made.filter((context) => !context.closed)).toEqual([])
    })

    it('rejects and lets go of everything when aborted before ready', async () => {
      const controller = new AbortController()
      const { connecting, socket } = await dialing({
        microphone: false,
        signal: controller.signal,
      })
      controller.abort()
      await expect(connecting).rejects.toEqual(aborted)
      expect(socket.readyState).toBe(3)
      expect(Context.made.filter((context) => !context.closed)).toEqual([])
    })

    it('rejects during the microphone prompt and stops the device as it arrives', async () => {
      const devices = media()
      vi.stubGlobal('navigator', devices.navigator)
      const held = promptHeld(devices)
      const controller = new AbortController()
      const { connecting, socket } = await dialing({ signal: controller.signal })
      socket.deliver({ type: 'ready' })
      await vi.waitFor(() => expect(devices.asked).toHaveLength(1))
      controller.abort()
      await expect(connecting).rejects.toEqual(aborted)
      expect(socket.readyState).toBe(3)
      held.answer()
      await vi.waitFor(() => expect(devices.tracks()).toHaveLength(1))
      await vi.waitFor(() => expect(devices.tracks()[0]?.stopped).toBe(true))
      expect(WorkletNode.made).toHaveLength(0)
    })

    it('rejects during the camera prompt and stops the device as it arrives', async () => {
      const devices = media()
      vi.stubGlobal('navigator', devices.navigator)
      vi.stubGlobal('document', documentWithCanvas())
      const held = promptHeld(devices)
      const controller = new AbortController()
      const { connecting, socket } = await dialing({
        microphone: false,
        camera: {},
        signal: controller.signal,
      })
      socket.deliver({ type: 'ready' })
      await vi.waitFor(() => expect(devices.asked).toHaveLength(1))
      controller.abort()
      await expect(connecting).rejects.toEqual(aborted)
      held.answer()
      await vi.waitFor(() => expect(devices.video()).toHaveLength(1))
      await vi.waitFor(() => expect(devices.video()[0]?.stopped).toBe(true))
    })

    it('raises nothing more when the person refuses a prompt after an abort', async () => {
      const devices = media()
      vi.stubGlobal('navigator', devices.navigator)
      const held = promptHeld(devices)
      const controller = new AbortController()
      const rejected = await unhandledDuring(async () => {
        const { connecting, socket } = await dialing({ signal: controller.signal })
        socket.deliver({ type: 'ready' })
        await vi.waitFor(() => expect(devices.asked).toHaveLength(1))
        controller.abort()
        await expect(connecting).rejects.toEqual(aborted)
        held.refuse()
      })
      expect(rejected).toEqual([])
    })

    it('does nothing once connect has resolved', async () => {
      const devices = media()
      vi.stubGlobal('navigator', devices.navigator)
      const controller = new AbortController()
      const connecting = connect({
        url: 'wss://example.com/senses',
        transport: 'websocket',
        signal: controller.signal,
      })
      await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
      const socket = Socket.made[0] as Socket
      socket.open()
      socket.deliver({ type: 'ready' })
      const senses = await connecting
      controller.abort()
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(senses.status).toBe('open')
      expect(socket.readyState).toBe(1)
      expect(devices.tracks().filter((track) => track.stopped)).toEqual([])
      await senses.close()
    })
  })

  it('closes the socket and rejects on a closed event before ready', async () => {
    const { connecting, socket } = await dialing({ microphone: false })
    socket.deliver({ type: 'event', event: closed })
    await expect(connecting).rejects.toEqual(early)
    expect(socket.readyState).toBe(3)
    expect((Context.made[0] as Context).closed).toBe(true)
  })

  it('closes the socket after a refused hello, whether or not the server does', async () => {
    const { connecting, socket } = await dialing({ microphone: false })
    socket.deliver({ type: 'error', message: 'transport: protocol 1 is not supported' })
    await expect(connecting).rejects.toEqual(
      new SensesException('transport: protocol 1 is not supported')
    )
    expect(socket.readyState).toBe(3)
  })

  it('resolves a second close, and a close after the far end, with nothing more to do', async () => {
    vi.stubGlobal('navigator', media().navigator)
    const twice = await opened({ microphone: true })
    await twice.senses.close()
    await expect(twice.senses.close()).resolves.toBeUndefined()
    expect(Context.made.map((context) => context.closes)).toEqual([1, 1])

    Socket.made = []
    Context.made = []
    const remote = await opened({ microphone: true })
    remote.socket.close()
    await expect(remote.senses.close()).resolves.toBeUndefined()
    await expect(remote.senses.close()).resolves.toBeUndefined()
    expect(Context.made.map((context) => context.closes)).toEqual([1, 1])
  })

  it('tells the closed event with the devices live, and the status once they are released', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
    const live = () => devices.tracks().filter((track) => !track.stopped).length
    const seen: Array<[string, number]> = []
    senses.subscribe((event) => {
      if (event.kind === 'closed') seen.push(['closed event', live()])
      if (event.kind === 'status') seen.push([event.status, live()])
    })
    socket.deliver({ type: 'event', event: closed })
    expect(seen).toEqual([
      ['open', 2],
      ['closed event', 2],
      ['closed', 0],
    ])
  })

  it('tells every subscriber the closed event and the status when the first throws on both', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
    const failed = { ...closed, reason: 'failed', message: 'models: the connection dropped' }
    const told = behindAThrowingSubscriber(senses)
    const rejected = await unhandledDuring(async () => {
      socket.deliver({ type: 'event', event: failed })
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
    expectReleased(devices, socket)
    expect(senses.status).toBe('closed')
  })

  it('tells every subscriber the status when the first throws on it and the socket closes', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
    const told = behindAThrowingSubscriber(senses)
    const rejected = await unhandledDuring(async () => {
      socket.close()
      await senses.close()
    })
    expect(told).toEqual([
      { kind: 'status', status: 'open' },
      { kind: 'status', status: 'closed' },
    ])
    expect(rejected).toEqual([])
    expect(() => vi.advanceTimersToNextTimer()).toThrow('a bug on status')
    expectReleased(devices, socket)
  })

  it('resolves close and tells every subscriber the status when the first throws on it', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
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
    expectReleased(devices, socket)
  })

  it('runs the stop once when a subscriber closes from inside the ending', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
    const closes: Array<Promise<void>> = []
    senses.subscribe((event) => {
      if (event.kind === 'closed') closes.push(senses.close())
      if (event.kind === 'status' && event.status === 'closed') closes.push(senses.close())
    })
    socket.deliver({ type: 'event', event: closed })
    await expect(Promise.all(closes)).resolves.toHaveLength(2)
    expectReleased(devices, socket)
    expect(Context.made.map((context) => context.closes)).toEqual([1, 1])
  })

  it('reopens no device and sets no timer after the far end', async () => {
    const { devices } = equipped()
    const { senses, socket } = await opened({ microphone: true, camera: { fps: 1 } })
    socket.close()
    const asked = devices.asked.length
    await senses.useCamera({ facing: 'user' })
    await senses.shareScreen()
    senses.setFps(4)
    senses.resumeCamera()
    senses.unmute()
    expect(devices.asked).toHaveLength(asked)
    expectReleased(devices, socket)
  })

  it('lets go of the old camera and sends no source when the connection ends during a switch', async () => {
    const { devices, page } = equipped()
    const { senses, socket } = await opened({ camera: { fps: 1 } })
    const sources: unknown[] = []
    senses.subscribe((event) => event.kind === 'video' && sources.push(event.source))
    let playing = () => {}
    const video = page.createElement('video') as { play: () => Promise<void> }
    video.play = () =>
      new Promise<void>((resolve) => {
        playing = resolve
      })
    const switching = senses.useCamera({ facing: 'user' })
    await vi.waitFor(() => expect(devices.video()).toHaveLength(2))
    socket.close()
    expect(devices.video().filter((track) => !track.stopped)).toEqual([])
    playing()
    await switching
    expect(sources).toEqual([])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('leaves no capture timer when the connection ends before the first frame plays', async () => {
    const { devices, page } = equipped()
    let playing = () => {}
    const video = page.createElement('video') as { play: () => Promise<void> }
    video.play = () =>
      new Promise<void>((resolve) => {
        playing = resolve
      })
    const { connecting, socket } = await dialing({ microphone: false, camera: { fps: 1 } })
    socket.deliver({ type: 'ready' })
    await vi.waitFor(() => expect(devices.video()).toHaveLength(1))
    socket.close()
    await expect(connecting).rejects.toEqual(early)
    playing()
    await vi.advanceTimersByTimeAsync(0)
    expect(vi.getTimerCount()).toBe(0)
  })
})
