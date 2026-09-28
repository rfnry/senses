import { describe, expect, it } from 'vitest'
import { type AudioContextLike, Playback } from '../src/playback'
import { Context } from './fakes'

const frame = (utterance: number, ms = 100) => ({
  utterance,
  sampleRate: 16000,
  channels: 1,
  pcm: new Uint8Array(32 * ms),
})

function setup() {
  const context = new Context()
  const reports: Array<[number, number]> = []
  const playback = new Playback(context as unknown as AudioContextLike, (u, ms) =>
    reports.push([u, ms])
  )
  return { context, reports, playback }
}

describe('playback', () => {
  it('reports what played when an utterance ends', () => {
    const { context, reports, playback } = setup()
    playback.push(frame(1))
    playback.push(frame(1))
    expect(context.nodes.map((n) => n.started)).toEqual([0.12, 0.22])
    playback.silent(1)
    expect(reports).toEqual([])
    for (const node of context.nodes) node.onended?.()
    expect(reports).toEqual([[1, 200]])
  })

  it('stops mid-utterance and reports how far it got', () => {
    const { context, reports, playback } = setup()
    for (let i = 0; i < 3; i++) playback.push(frame(1))
    context.nodes[0]?.onended?.()
    context.currentTime = 0.27
    playback.stop(1)
    expect(reports).toEqual([[1, 150]])
    expect(context.nodes.slice(1).every((n) => n.stopped)).toBe(true)
  })

  it('fades out instead of cutting, and the next utterance plays at full volume', () => {
    const { context, playback } = setup()
    playback.push(frame(1))
    playback.push(frame(1))
    context.currentTime = 0.17
    playback.stop(1)
    expect(context.gains[0]?.ramps).toEqual([[0, 0.23]])
    expect(context.nodes.map((n) => n.stoppedAt)).toEqual([0.23, 0.23])
    playback.push(frame(2))
    expect(context.gains).toHaveLength(2)
    expect(context.gains[1]?.ramps).toEqual([])
  })

  it('starts a stretch of voice 120 ms ahead, so a late second chunk leaves no hole', () => {
    const { context, playback } = setup()
    playback.push(frame(1, 40))
    context.currentTime = 0.1
    playback.push(frame(1))
    expect(context.nodes.map((n) => n.started)).toEqual([0.12, 0.16])
    context.currentTime = 0.2
    playback.push(frame(2))
    expect(context.nodes[2]?.started).toBeCloseTo(0.26)
    context.currentTime = 1
    playback.push(frame(3))
    expect(context.nodes[3]?.started).toBeCloseTo(1.12)
  })

  it('counts only audio that played, not audio still buffered, and a stop takes effect at once', () => {
    const { context, reports, playback } = setup()
    playback.push(frame(1))
    playback.push(frame(1))
    context.currentTime = 0.1
    playback.stop(1)
    expect(reports).toEqual([[1, 0]])
    expect(context.gains[0]?.ramps).toEqual([[0, 0.16]])
    expect(context.nodes.map((n) => n.stoppedAt)).toEqual([0.16, 0.22])
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
