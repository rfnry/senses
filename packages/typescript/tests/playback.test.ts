import { describe, expect, it } from 'vitest'
import { type AudioContextLike, Playback } from '../src/playback'

class Node {
  buffer: unknown = null
  onended: (() => void) | null = null
  started = -1
  stopped = false
  connect(): void {}
  start(at: number): void {
    this.started = at
  }
  stop(): void {
    this.stopped = true
  }
}

class Context {
  currentTime = 0
  destination = {} as AudioNode
  nodes: Node[] = []
  createBuffer(_channels: number, length: number, rate: number): AudioBuffer {
    return { duration: length / rate, getChannelData: () => new Float32Array(length) } as unknown as AudioBuffer
  }
  createBufferSource(): AudioBufferSourceNode {
    const node = new Node()
    this.nodes.push(node)
    return node as unknown as AudioBufferSourceNode
  }
  async close(): Promise<void> {}
}

const frame = (utterance: number, ms = 100) => ({
  utterance,
  sampleRate: 16000,
  channels: 1,
  pcm: new Uint8Array(32 * ms),
})

function setup() {
  const context = new Context()
  const reports: Array<[number, number]> = []
  const playback = new Playback(context as unknown as AudioContextLike, (u, ms) => reports.push([u, ms]))
  return { context, reports, playback }
}

describe('playback', () => {
  it('reports what played when an utterance ends', () => {
    const { context, reports, playback } = setup()
    playback.push(frame(1))
    playback.push(frame(1))
    expect(context.nodes.map((n) => n.started)).toEqual([0, 0.1])
    playback.silent(1)
    expect(reports).toEqual([])
    for (const node of context.nodes) node.onended?.()
    expect(reports).toEqual([[1, 200]])
  })

  it('stops mid-utterance and reports how far it got', () => {
    const { context, reports, playback } = setup()
    for (let i = 0; i < 3; i++) playback.push(frame(1))
    context.nodes[0]?.onended?.()
    context.currentTime = 0.15
    playback.stop(1)
    expect(reports).toEqual([[1, 150]])
    expect(context.nodes.slice(1).every((n) => n.stopped)).toBe(true)
  })

  it('drops audio of a stopped utterance', () => {
    const { context, reports, playback } = setup()
    playback.push(frame(1))
    playback.stop(1)
    playback.push(frame(1))
    expect(context.nodes).toHaveLength(1)
    playback.push(frame(2))
    expect(context.nodes).toHaveLength(2)
    playback.stop(5)
    expect(reports).toEqual([
      [1, 0],
      [2, 0],
      [5, 0],
    ])
  })

  it('closing reports what was playing and nothing more', async () => {
    const { reports, playback } = setup()
    playback.push(frame(1))
    await playback.close()
    expect(reports).toEqual([[1, 0]])
  })
})
