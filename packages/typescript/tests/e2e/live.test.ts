import { afterEach, beforeAll, describe, it } from 'vitest'
import {
  expectTheRoundTrip,
  needsUv,
  start,
  stop,
  synthesize,
  talk,
  waitFor,
} from '../python-server'

if (!process.env.RFNRY_LIVE_ADAPTERS)
  throw new Error(
    'tests: the live tier needs RFNRY_LIVE_ADAPTERS, the module path of your live adapters'
  )

afterEach(stop)
beforeAll(needsUv)

describe('the TypeScript client against real transcription and speech', () => {
  it('is heard by the transcription model and answered by the speech model', async () => {
    const { rate, pcm } = synthesize('A table for two at eight.')
    const server = await start(true)
    const step = (rate / 10) * 2
    let answered = false
    const result = await talk(server, async (link) => {
      const speech = [...Array(Math.ceil(pcm.length / step)).keys()].map((i) =>
        pcm.slice(i * step, (i + 1) * step)
      )
      for (const chunk of speech) {
        link.sendAudio(chunk, rate)
        await new Promise((resolve) => setTimeout(resolve, 100))
      }
      while (!answered) {
        link.sendAudio(new Uint8Array(step), rate)
        await new Promise((resolve) => setTimeout(resolve, 100))
      }
    })
    answered = true
    expectTheRoundTrip(result, 'table')
    result.link?.close()
    await waitFor(server.output, /closed hung_up/)
  })
})
