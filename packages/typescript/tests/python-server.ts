import { type ChildProcess, execFileSync, execSync, spawn } from 'node:child_process'
import { join } from 'node:path'
import { expect } from 'vitest'
import { type AudioContextLike, Playback } from '../src/playback'
import { WebSocketLink } from '../src/websocket'
import type { AudioOut, SensesEvent, ServerControl } from '../src/wire'
import { Context } from './fakes'

const PYTHON = join(import.meta.dirname, '../../python')
const hasUv = (() => {
  try {
    execSync('uv --version', { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
})()

type Server = { process: ChildProcess; port: number; output: string[] }
let running: Server | null = null

export function waitFor(
  output: string[],
  pattern: RegExp,
  timeout = 30_000
): Promise<RegExpMatchArray> {
  return new Promise((resolve, reject) => {
    const started = Date.now()
    const check = () => {
      const found = output.join('').match(pattern)
      if (found) return resolve(found)
      if (Date.now() - started > timeout)
        return reject(new Error(`no ${pattern} in: ${output.join('')}`))
      setTimeout(check, 20)
    }
    check()
  })
}

export async function start(live: boolean): Promise<Server> {
  const env: NodeJS.ProcessEnv = { ...process.env }
  if (!live) delete env.RFNRY_LIVE_ADAPTERS
  const child = spawn('uv', ['run', 'python', 'tests/integration/serve_for_client.py', '0'], {
    cwd: PYTHON,
    env,
  })
  const output: string[] = []
  child.stdout?.on('data', (chunk) => output.push(String(chunk)))
  child.stderr?.on('data', (chunk) => output.push(String(chunk)))
  running = { process: child, port: 0, output }
  running.port = Number((await waitFor(output, /listening (\d+)/))[1])
  return running
}

export function synthesize(text: string): { rate: number; pcm: Uint8Array } {
  const script = `import asyncio, base64, importlib, os
adapters = importlib.import_module(os.environ["RFNRY_LIVE_ADAPTERS"])
async def main():
    frames = [f async for f in adapters.speech().speak(${JSON.stringify(text)})]
    print(frames[0].sample_rate, base64.b64encode(b"".join(f.data for f in frames)).decode())
asyncio.run(main())`
  const out = execFileSync('uv', ['run', 'python', '-c', script], {
    cwd: PYTHON,
    maxBuffer: 64 * 1024 * 1024,
  })
  const [rate, data] = String(out).trim().split('\n').at(-1)?.split(' ') ?? []
  return { rate: Number(rate), pcm: Uint8Array.from(Buffer.from(data ?? '', 'base64')) }
}

export async function talk(server: Server, send: (link: WebSocketLink) => Promise<void>) {
  const seen: string[] = []
  const events: SensesEvent[] = []
  const audio: AudioOut[] = []
  const context = new Context()
  let link: WebSocketLink | null = null
  const playback = new Playback(context as unknown as AudioContextLike, (utterance, ms) =>
    link?.played(utterance, ms)
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
    void WebSocketLink.open(`ws://127.0.0.1:${server.port}`, undefined, handlers).then((opened) => {
      link = opened
      void send(opened)
    })
  })
  const event = await said
  return { event, events, seen, audio, link: link as WebSocketLink | null }
}

export function expectTheRoundTrip(result: Awaited<ReturnType<typeof talk>>, words: string) {
  expect(result.event.kind === 'said' && result.event.heard).toBe('Hi there.')
  const heard = result.events.find((e) => e.kind === 'heard' && e.final)
  expect(heard?.kind === 'heard' && heard.text.toLowerCase()).toContain(words)
  const order = ['heard', 'turn_ended', 'audio 1', 'said'].map((kind) => result.seen.indexOf(kind))
  expect(order.every((index) => index >= 0)).toBe(true)
  expect([...order].sort((a, b) => a - b)).toEqual(order)
  expect(result.audio.every((frame) => frame.utterance === 1)).toBe(true)
}

export function stop() {
  running?.process.kill()
  running = null
}

export function needsUv() {
  if (!hasUv)
    throw new Error('tests: the integration tier needs uv on the PATH to run the Python server')
}
