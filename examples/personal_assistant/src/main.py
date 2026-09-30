from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routes import router

CLIENT = Path(__file__).resolve().parents[3] / "packages" / "typescript" / "dist"

app = FastAPI()
app.include_router(router)
app.mount("/client", StaticFiles(directory=CLIENT, check_dir=False))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
