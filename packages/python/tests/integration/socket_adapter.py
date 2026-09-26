from fastapi import WebSocket, WebSocketDisconnect


class Socket:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.send_text = ws.send_text
        self.send_bytes = ws.send_bytes
        self.close = ws.close

    async def receive(self) -> str | bytes:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        return message["text"] if message.get("text") is not None else message["bytes"]
