import asyncio
import sys

import uvicorn
from rfnry_senses import Identity, Loopback

from client import guest_says
from harness import converse
from server import ORIGIN, app


async def headless() -> None:
    link = Loopback(Identity(role="user", id="ana", name="Ana"), scope="acme")
    talking = asyncio.create_task(converse(link, replies=1))
    await guest_says(link, "Hi! Do you have a table for two at eight tonight?")
    await asyncio.wait_for(talking, 60)
    print(
        f"the guest's line received {sum(f.duration_ms for f in link.audio) / 1000:.1f} s of voice"
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["browser"]:
        print(f"open {ORIGIN}")
        uvicorn.run(app, host="localhost", port=8000)
    else:
        asyncio.run(headless())
