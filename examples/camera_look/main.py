import asyncio
import sys
from pathlib import Path

import uvicorn
from rfnry_senses import Loopback, Session

from harness import ask
from server import ANA, ORIGIN, app


async def headless() -> None:
    link = Loopback(ANA, scope="acme")
    async with Session(link, video=True) as session:
        await link.show((Path(__file__).parent / "shapes.jpg").read_bytes())
        await asyncio.sleep(0.1)
        for question in ("What do you see?", "Which shape is on the left, and what colour is it?"):
            print(f"asked    {question}")
            print(f"answered {await ask(session, question)}")


if __name__ == "__main__":
    if sys.argv[1:] == ["browser"]:
        print(f"open {ORIGIN}")
        uvicorn.run(app, host="localhost", port=8000)
    else:
        asyncio.run(headless())
