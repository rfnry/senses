import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connect, type Senses, SensesError, type SensesListener } from '../src/index'
import { encodeAudio, encodeImage, hello, played } from '../src/wire'
import { Context, documentWithCanvas, JPEG, media, Socket, WorkletNode } from './fakes'

const ANA = { role: 'user', id: 'ana', name: 'Ana', metadata: {} }
const at = { scope: 'acme', at: '2026-09-24T12:03:04.200000Z' }

function audio(utterance: number, ms = 100): ArrayBuffer {
  const frame = encodeAudio(new Uint8Array(32 * ms), 16000, 1, utterance)
  return frame.buffer.slice(frame.byteOffset, frame.byteOffset + frame.byteLength) as ArrayBuffer
}

async function opened(
  options: Partial<Parameters<typeof connect>[0]> = {},
): Promise<{ senses: Senses; socket: Socket }> {
  const connecting = connect({ url: 'wss://example.com/senses', transport: 'websocket', microphone: false, ...options })
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
    const connecting = connect({ url: 'wss://example.com/senses', transport: 'websocket', microphone: false })
    await vi.waitFor(() => expect(Socket.made).toHaveLength(1))
    const socket = Socket.made[0] as Socket
    socket.open()
    socket.deliver({ type: 'error', message: 'transport: protocol 2 is not supported; this server speaks 1' })
    await expect(connecting).rejects.toEqual(
      new SensesError('transport: protocol 2 is not supported; this server speaks 1'),
    )
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
    expect(() => senses.setFps(0)).toThrow(new SensesError('senses: fps must be positive'))
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

  it('refuses a screen without the camera option', async () => {
    const { senses } = await opened()
    await expect(senses.shareScreen()).rejects.toEqual(new SensesError('senses: shareScreen needs the camera option'))
    await expect(senses.useCamera()).rejects.toEqual(new SensesError('senses: useCamera needs the camera option'))
  })

  it('refuses a rate without a camera', async () => {
    const { senses } = await opened()
    expect(() => senses.setFps(2)).toThrow(new SensesError('senses: setFps needs a camera'))
  })

  it('closes on the server closed event, on the socket closing, and on close()', async () => {
    const first = await opened()
    const statuses: string[] = []
    first.senses.subscribe((event) => event.kind === 'status' && statuses.push(event.status))
    first.socket.deliver({ type: 'event', event: { ...at, kind: 'closed', reason: 'hung_up', message: null } })
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
