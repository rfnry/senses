import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_senses import Identity
from rfnry_senses.webrtc import accept_offer

from harness import converse

ORIGIN = "http://localhost:8000"
CLIENT = Path(__file__).parents[2] / "packages/typescript/dist"
TOKEN = "ana-demo-token"
ANA = Identity(role="user", id="ana", name="Ana")
tasks: set[asyncio.Task[None]] = set()


def signed_in(request: Request) -> Identity:
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token != TOKEN and request.cookies.get("guest") != "ana":
        raise HTTPException(status_code=401)
    return ANA


app = FastAPI()
app.mount("/client", StaticFiles(directory=CLIENT, check_dir=False), name="client")


@app.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse((Path(__file__).parent / "static/index.html").read_text())
    response.set_cookie("guest", "ana", httponly=True, samesite="strict")
    return response


@app.post("/senses/offer")
async def offer(body: dict, guest: Identity = Depends(signed_in)) -> dict:
    link = await accept_offer(body["sdp"], identity=guest, scope="acme")
    task = asyncio.create_task(converse(link))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"sdp": link.answer}
