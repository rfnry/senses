import asyncio
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rfnry_senses import Identity
from rfnry_senses.webrtc import accept_offer

import harness

Tenant = Annotated[str, Header(alias="X-Tenant")]
ORIGIN = "http://localhost:8000"
KIM = Identity(role="user", id="kim", name="Kim")
calls: set[asyncio.Task[None]] = set()


class Offer(BaseModel):
    sdp: str


app = FastAPI()
app.mount("/client", StaticFiles(directory="../../packages/typescript/dist", check_dir=False))


@app.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse(Path("static/index.html").read_text())
    response.set_cookie("staff", "kim", httponly=True, samesite="strict")
    return response


@app.post("/senses/offer")
async def offer(body: Offer, request: Request, clinic: Tenant) -> dict:
    if request.headers.get("origin") != ORIGIN or request.cookies.get("staff") != "kim":
        raise HTTPException(403)
    link = await accept_offer(body.sdp, identity=KIM, scope=clinic)
    call = asyncio.create_task(harness.converse(link))
    calls.add(call)
    call.add_done_callback(calls.discard)
    return {"sdp": link.answer}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
