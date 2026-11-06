import { describe, expect, it } from 'vitest'
import { downsample, toPcm16 } from '../src/microphone'

const fresh = () => ({ tail: new Float32Array(0), position: 0 })

const tone = (hz: number, rate: number, length: number) =>
  Float32Array.from({ length }, (_, i) => Math.sin((2 * Math.PI * hz * i) / rate))

const level = (samples: Float32Array) =>
  Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length)

const through = (hz: number, inRate: number, outRate: number) =>
  level(downsample(fresh(), tone(hz, inRate, inRate), inRate, outRate).slice(1000))

const decibels = (ratio: number) => 20 * Math.log10(ratio)

describe('the microphone', () => {
  it('the microphone downsampler does not alias', () => {
    expect(
      decibels(through(10000, 48000, 16000) / through(1000, 48000, 16000))
    ).toBeLessThanOrEqual(-40)
  })

  it('builds its filter for the rates it is given', () => {
    expect(
      decibels(through(10000, 44100, 16000) / through(1000, 44100, 16000))
    ).toBeLessThanOrEqual(-40)
    expect(decibels(through(8000, 48000, 24000) / through(1000, 48000, 24000))).toBeCloseTo(0, 1)
    expect(
      decibels(through(14000, 48000, 24000) / through(1000, 48000, 24000))
    ).toBeLessThanOrEqual(-40)
  })

  it('keeps speech at its level, in step with the input', () => {
    const passed = through(1000, 48000, 16000) / level(tone(1000, 48000, 48000))
    expect(decibels(passed)).toBeCloseTo(0, 1)
    const ramp = Float32Array.from({ length: 4800 }, (_, i) => i)
    const out = downsample(fresh(), ramp, 48000, 16000)
    expect(out[1000]).toBeCloseTo(3000, 1)
  })

  it('sends the rate it was asked for, after a few samples of delay', () => {
    const state = fresh()
    let samples = 0
    for (let i = 0; i < 480; i++)
      samples += downsample(state, new Float32Array(100), 48000, 16000).length
    expect(16000 - samples).toBeGreaterThanOrEqual(0)
    expect(16000 - samples).toBeLessThanOrEqual(32)
  })

  it("upsamples to a rate above the browser's, in step and at its level", () => {
    const out = downsample(fresh(), tone(1000, 16000, 16000), 16000, 48000)
    expect(48000 - out.length).toBeGreaterThanOrEqual(0)
    expect(48000 - out.length).toBeLessThanOrEqual(96)
    expect(decibels(through(1000, 16000, 48000) / level(tone(1000, 16000, 16000)))).toBeCloseTo(
      0,
      1
    )
    const expected = tone(1000, 48000, 48000)
    const apart = out
      .slice(1000, 40000)
      .reduce((most, sample, i) => Math.max(most, Math.abs(sample - (expected[i + 1000] ?? 0))), 0)
    expect(apart).toBeLessThan(0.01)
  })

  it('passes audio through when the rates match', () => {
    const signal = tone(1000, 48000, 480)
    const out = downsample(fresh(), signal, 48000, 48000)
    for (let i = 0; i < out.length; i++) expect(out[i]).toBeCloseTo(signal[i] ?? 0, 5)
  })

  it('the downsampler is continuous across chunks', () => {
    for (const rate of [48000, 44100]) {
      const signal = tone(1000, rate, rate).map((sample, i) => sample + Math.sin(i * 0.37))
      const whole = downsample(fresh(), signal, rate, 16000)
      const state = fresh()
      const parts: number[] = []
      for (let i = 0; i < signal.length; i += 128)
        parts.push(...downsample(state, signal.subarray(i, i + 128), rate, 16000))
      expect(parts.length).toBe(whole.length)
      const apart = parts.reduce(
        (most, sample, i) => Math.max(most, Math.abs(sample - (whole[i] ?? 0))),
        0
      )
      expect(apart).toBeLessThan(1e-6)
    }
  })

  it('runs from its own source, as the worklet builds it', () => {
    const rebuilt = new Function(`return ${String(downsample)}`)() as typeof downsample
    const signal = tone(1000, 44100, 4410)
    expect(Array.from(rebuilt(fresh(), signal, 44100, 16000))).toEqual(
      Array.from(downsample(fresh(), signal, 44100, 16000))
    )
  })

  it('writes clipped little-endian 16-bit samples', () => {
    const pcm = toPcm16(Float32Array.of(1, -1, 0.5, 2))
    expect(Buffer.from(pcm).toString('hex')).toBe('ff7f0080ff3fff7f')
  })
})
