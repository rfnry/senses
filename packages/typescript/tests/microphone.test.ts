import { describe, expect, it } from 'vitest'
import { downsample, toPcm16 } from '../src/microphone'

describe('the microphone', () => {
  it('downsamples 48 kHz to 16 kHz, three to one', () => {
    const state = { tail: new Float32Array(0), position: 0 }
    const input = Float32Array.from({ length: 480 }, (_, i) => i)
    const out = downsample(state, input, 48000, 16000)
    expect(out.length).toBe(160)
    expect(Array.from(out.slice(0, 3))).toEqual([0, 3, 6])
  })

  it('keeps its fractional position across calls', () => {
    const whole = Float32Array.from({ length: 900 }, (_, i) => i)
    const once = downsample({ tail: new Float32Array(0), position: 0 }, whole, 44100, 16000)
    const state = { tail: new Float32Array(0), position: 0 }
    const first = downsample(state, whole.slice(0, 450), 44100, 16000)
    const second = downsample(state, whole.slice(450), 44100, 16000)
    expect(Array.from([...first, ...second])).toEqual(Array.from(once))
  })

  it('writes clipped little-endian 16-bit samples', () => {
    const pcm = toPcm16(Float32Array.of(1, -1, 0.5, 2))
    expect(Buffer.from(pcm).toString('hex')).toBe('ff7f0080ff3fff7f')
  })
})
