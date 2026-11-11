"""Opus audio from the glasses (battery: ~4 KB/s at 32 kbps instead of 32 KB/s of PCM).

Each binary WebSocket message holds one or more Opus packets, each preceded by its length as a
2-byte big-endian number. Decoded with PyAV (already installed for faster-whisper) into the
same 16 kHz mono int16 PCM the rest of the server uses. Round trip checked on our clips: the
same transcripts as from the PCM (one word differed in a local Whisper partial).
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)


def split_packets(data: bytes) -> list[bytes]:
    packets, i = [], 0
    while i + 2 <= len(data):
        (n,) = struct.unpack_from(">H", data, i)
        i += 2
        if n == 0 or i + n > len(data):
            break
        packets.append(data[i:i + n])
        i += n
    return packets


def frame_packets(packets: list[bytes]) -> bytes:
    """The glasses' framing (also used by tests and tools)."""
    return b"".join(struct.pack(">H", len(p)) + p for p in packets)


class OpusDecoder:
    def __init__(self) -> None:
        import av

        self._av = av
        self._codec = av.CodecContext.create("opus", "r")
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        self._logged = 0

    def decode(self, data: bytes) -> bytes:
        # The first messages, for checking what an encoder really sends (the glasses' MediaCodec
        # output decoded into loud noise, 2026-11-11).
        if self._logged < 3:
            self._logged += 1
            packets = split_packets(data)
            log.info("opus message %d: %d bytes, %d packets %s, head %s", self._logged, len(data),
                     len(packets), [len(p) for p in packets][:8], data[:24].hex())
        out = []
        for packet in split_packets(data):
            try:
                for frame in self._codec.decode(self._av.Packet(packet)):
                    for resampled in self._resampler.resample(frame):
                        out.append(bytes(resampled.planes[0])[: resampled.samples * 2])
            except Exception as e:  # one bad packet must not end the session
                log.info("opus packet dropped: %s", e)
        return b"".join(out)
