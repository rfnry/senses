import { type ChildProcess, execSync, spawn } from 'node:child_process'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { type AudioContextLike, Playback } from '../../src/playback'
import { WebSocketLink } from '../../src/websocket'
import type { AudioOut, SensesEvent, ServerControl } from '../../src/wire'
import { Context } from '../fakes'

const PYTHON = join(__dirname, '../../../python')
const hasUv = (() => {
  try {
    execSync('uv --version', { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
})()

let server: ChildProcess
let port = 0
const output: string[] = []

function waitFor(pattern: RegExp, timeout = 30_000): Promise<RegExpMatchArray> {
  return new Promise((resolve, reject) => {
    const started = Date.now()
    const check = () => {
      const found = output.join('').match(pattern)
      if (found) return resolve(found)
      if (Date.now() - started > timeout) return reject(new Error(`no ${pattern} in: ${output.join('')}`))
      setTimeout(check, 20)
    }
    check()
  })
}

describe.skipIf(!hasUv)('the TypeScript client against the Python server', () => {
  beforeAll(async () => {
    server = spawn('uv', ['run', 'python', 'tests/integration/serve_for_client.py', '0'], {
      cwd: PYTHON,
      env: process.env,
    })
    server.stdout?.on('data', (chunk) => output.push(String(chunk)))
    server.stderr?.on('data', (chunk) => output.push(String(chunk)))
    port = Number((await waitFor(/listening (\d+)/))[1])
  })

  afterAll(() => {
    server?.kill()
  })

  it('is heard, answered, and hung up on', async () => {
    const seen: string[] = []
    const events: SensesEvent[] = []
    const audio: AudioOut[] = []
    const context = new Context()
    let link: WebSocketLink | null = null
    const playback = new Playback(context as unknown as AudioContextLike, (utterance, ms) =>
      link?.played(utterance, ms),
    )
    const said = new Promise<SensesEvent>((resolve) => {
      const handlers = {
        control(frame: ServerControl) {
          if (frame.type === 'speaking') playback.speaking(frame.utterance)
          if (frame.type === 'silent') {
            playback.silent(frame.utterance)
            for (const node of context.nodes) node.onended?.()
          }
          if (frame.type === 'event') {
            events.push(frame.event)
            seen.push(frame.event.kind)
            if (frame.event.kind === 'said') resolve(frame.event)
          }
        },
        audio(frame: AudioOut) {
          seen.push(`audio ${frame.utterance}`)
          audio.push(frame)
          playback.push(frame)
        },
        closed() {},
      }
      void WebSocketLink.open(`ws://127.0.0.1:${port}`, undefined, handlers).then((opened) => {
        link = opened
        for (let i = 0; i < 20; i++) opened.sendAudio(new Uint8Array(640), 16000)
      })
    })
    const event = await said
    expect(event.kind === 'said' && event.heard).toBe('Hi there.')
    const heard = events.find((e) => e.kind === 'heard')
    expect(heard?.kind === 'heard' && heard.text).toBe('hello')
    const order = ['heard', 'turn_ended', 'audio 1', 'said'].map((kind) => seen.indexOf(kind))
    expect(order.every((index) => index >= 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    expect(audio.every((frame) => frame.utterance === 1)).toBe(true)
    ;(link as WebSocketLink | null)?.close()
    await waitFor(/closed hung_up/)
  })
})
