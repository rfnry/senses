import asyncio

from rfnry_senses import AudioFrame, Loopback

from harness import voice


async def guest_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    silence = AudioFrame(data=bytes(4800), sample_rate=24000)
    for _ in range(80):
        await link.speak(silence)
        await asyncio.sleep(0.1)
