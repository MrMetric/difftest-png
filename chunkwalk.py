"""The chunk layout of a file's bytes, read without the reference decoder: what the report draws as a strip.

walk(data) -> {"signature": bool, "chunks": [...], "trailer": int}. The walk starts at offset 8 whether or
not the signature is right and does not stop at IEND. Each chunk is {"type", "length", "crc", "truncated"}:
`type` is the four type bytes as latin-1 with every byte outside 0x20-0x7E written as \\xNN; `crc` is "ok",
"bad", or "none" for a chunk whose declared length runs past the end of the data, which is the last one and
`truncated`. Fewer than 8 bytes left for a length and a type ends the walk, and `trailer` is their count.
"""
import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def type_text(t: bytes) -> str:
	assert len(t) == 4, t
	return "".join(chr(b) if 0x20 <= b <= 0x7E else f"\\x{b:02x}" for b in t)


def walk(data: bytes) -> dict:
	chunks = []
	pos = 8
	while True:
		left = len(data) - pos
		if left < 8:
			return {"signature": data[:8] == SIGNATURE, "chunks": chunks, "trailer": max(left, 0)}
		length, = struct.unpack(">I", data[pos:pos + 4])
		ctype = data[pos + 4:pos + 8]
		end = pos + 8 + length + 4
		if end > len(data):
			chunks.append({"type": type_text(ctype), "length": length, "crc": "none", "truncated": True})
			return {"signature": data[:8] == SIGNATURE, "chunks": chunks, "trailer": 0}
		stored, = struct.unpack(">I", data[end - 4:end])
		crc = "ok" if zlib.crc32(data[pos + 4:end - 4]) == stored else "bad"
		chunks.append({"type": type_text(ctype), "length": length, "crc": crc, "truncated": False})
		pos = end
