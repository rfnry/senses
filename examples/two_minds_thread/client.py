import asyncio

from rfnry_senses import AudioFrame, Loopback

from providers import GoogleSpeech

voice = GoogleSpeech(voice="Puck")


async def guest_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    for _ in range(20):
        await link.speak(AudioFrame(data=bytes(4800), sample_rate=24000))
        await asyncio.sleep(0.1)
