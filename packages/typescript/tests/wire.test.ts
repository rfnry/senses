import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { SensesException } from '../src/errors'
import { decodeAudio, encodeAudio, encodeImage, hello, parseControl, played } from '../src/wire'

const FIXTURES = join(import.meta.dirname, '../../python/tests/fixtures/wire')
const json = (name: string): unknown =>
  JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), 'utf8'))
const hex = (name: string): Uint8Array =>
  Uint8Array.from(Buffer.from(readFileSync(join(FIXTURES, `${name}.hex`), 'utf8').trim(), 'hex'))

describe('the wire', () => {
  it('writes the client frames the server expects', () => {
    expect(JSON.parse(hello())).toEqual(json('hello'))
    expect(JSON.parse(played(3, 1200))).toEqual(json('played'))
    expect(encodeAudio(Uint8Array.of(0x00, 0x01, 0xff, 0x7f), 16000, 1, 3)).toEqual(hex('audio'))
    expect(encodeImage(Uint8Array.of(0xff, 0xd8, 0xff, 0xd9))).toEqual(hex('image'))
  })

  it('reads every server frame', () => {
    for (const name of ['ready', 'speaking', 'silent', 'stop', 'error']) {
      expect(parseControl(JSON.stringify(json(name)))).toEqual(json(name))
    }
    for (const kind of [
      'heard',
      'turn_ended',
      'said',
      'interrupted',
      'speaking',
      'silent',
      'looked',
      'muted',
      'closed',
    ]) {
      const control = parseControl(JSON.stringify(json(`event-${kind}`)))
      expect(control.type === 'event' && control.event.kind).toBe(kind)
    }
    const audio = decodeAudio(hex('audio').buffer as ArrayBuffer)
    expect(audio).toMatchObject({ utterance: 3, sampleRate: 16000, channels: 1 })
    expect(Array.from(audio.pcm)).toEqual([0x00, 0x01, 0xff, 0x7f])
  })

  it('refuses what it cannot read', () => {
    expect(() => parseControl('{"type":"nope"}')).toThrow(
      new SensesException('wire: unknown control frame nope')
    )
    expect(() => decodeAudio(new Uint8Array([2, 1]).buffer)).toThrow(
      new SensesException('wire: not an audio frame')
    )
    expect(() => decodeAudio(new ArrayBuffer(0))).toThrow(
      expect.objectContaining({ name: 'SensesException' })
    )
  })
})
