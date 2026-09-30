import asyncio
import sys

import uvicorn
from rfnry_senses import Loopback

from client import ana_says, desk, film, screen
from harness import companion, narrate
from server import ANA, ORIGIN, app


async def headless(frame: bytes, question: str) -> None:
    link = Loopback(ANA, scope="home")
    answered = asyncio.Event()
    async with await companion(link) as session:
        narrating = asyncio.create_task(narrate(session, answered))
        filming = asyncio.create_task(film(link, frame))
        await asyncio.sleep(1)
        await ana_says(link, question)
        await asyncio.wait_for(answered.wait(), 90)
        await asyncio.sleep(0.5)
        narrating.cancel()
        filming.cancel()


if __name__ == "__main__":
    if sys.argv[1:] == ["browser"]:
        print(f"open {ORIGIN}")
        uvicorn.run(app, host="localhost", port=8000)
    elif sys.argv[1:] == ["screen"]:
        asyncio.run(
            headless(screen(), "I'm sharing my screen. I need to save this, where do I click?")
        )
    else:
        asyncio.run(headless(desk(), "What do you see on my desk?"))
