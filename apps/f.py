import asyncio

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()


async def fake_video_streamer():
    for i in range(100):
        yield b"bb"
        await asyncio.sleep(1)


@app.get("/")
async def main():
    return StreamingResponse(fake_video_streamer(), media_type="text/event-stream")