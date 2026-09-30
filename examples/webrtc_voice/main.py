import asyncio
import sys

import uvicorn

from client import call
from harness import answered
from server import ORIGIN, app


async def headless() -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="localhost", port=8000, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    pc = await call("Hi! Until when is the terrace open tonight?")
    await asyncio.wait_for(answered.wait(), 60)
    await pc.close()
    await asyncio.sleep(0.5)
    server.should_exit = True
    await serving


if __name__ == "__main__":
    if sys.argv[1:] == ["browser"]:
        print(f"open {ORIGIN}")
        uvicorn.run(app, host="localhost", port=8000)
    else:
        asyncio.run(headless())
