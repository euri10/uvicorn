import asyncio

from litestar import Litestar, get
from litestar.response import Stream


async def fake_video_streamer():
    for i in range(10):
        yield (b"1")
        await asyncio.sleep(0.1)


@get("/")
async def main() -> Stream:
    return Stream(fake_video_streamer(), media_type="text/event-stream")


app = Litestar(route_handlers=[main])

