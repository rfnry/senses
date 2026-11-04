import { afterEach, beforeAll, describe, it } from 'vitest'
import { expectTheRoundTrip, needsUv, start, stop, talk, waitFor } from '../python-server'

afterEach(stop)
beforeAll(needsUv)

describe('the TypeScript client against the Python server', () => {
  it('is heard, answered, and hung up on', async () => {
    const server = await start(false)
    const result = await talk(server, async (link) => {
      for (let i = 0; i < 20; i++) link.sendAudio(new Uint8Array(640), 16000)
    })
    expectTheRoundTrip(result, 'hello')
    result.link?.close()
    await waitFor(server.output, /closed hung_up/)
  })
})
