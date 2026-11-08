"""Stream a WAV file to the server in real time, the way the glasses do, and print what comes back.

    python tools/send_wav.py speech.wav --url ws://localhost:8000/ws --token "$CONVERSATE_TOKEN"

The file must be 16 kHz mono 16-bit. Convert with:
    ffmpeg -i in.m4a -ar 16000 -ac 1 -sample_fmt s16 speech.wav
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import wave

import websockets

CHUNK_MS = 100


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("wav")
    p.add_argument("--url", default="ws://localhost:8000/ws")
    p.add_argument("--token", default=os.environ.get("CONVERSATE_TOKEN", ""))
    p.add_argument("--speed", type=float, default=1.0, help="2 = twice real time")
    p.add_argument("--wait", type=float, default=30.0, help="seconds to wait for the last caption")
    args = p.parse_args()

    with wave.open(args.wav, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit("need 16 kHz mono 16-bit WAV (see the ffmpeg line in this file)")
        pcm = w.readframes(w.getnframes())

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with websockets.connect(args.url, additional_headers=headers) as ws:
        await ws.send(json.dumps({"type": "hello", "sample_rate": 16000, "format": "pcm_s16le", "levels": True}))
        start = time.monotonic()
        done = asyncio.Event()

        async def receive() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                t = time.monotonic() - start
                if msg["type"] == "caption":
                    tag = "FINAL  " if msg["final"] else "partial"
                    print(f"{t:6.2f}s {tag} #{msg['id']}: {msg['text']}", flush=True)
                    if msg["final"] and done.is_set():
                        return
                elif msg["type"] != "level":
                    print(f"{t:6.2f}s {msg}", flush=True)

        receiver = asyncio.create_task(receive())
        step = 16000 * 2 * CHUNK_MS // 1000
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i : i + step])
            await asyncio.sleep(CHUNK_MS / 1000 / args.speed)
        await ws.send(json.dumps({"type": "stop"}))
        done.set()
        try:
            await asyncio.wait_for(receiver, args.wait)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
