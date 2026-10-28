"""Minimal PAM (P7) reader, the decoder-output interchange format, and the canonical pixel form."""
import hashlib
from dataclasses import dataclass

import numpy as np


@dataclass
class PAM:
	width: int
	height: int
	depth: int
	maxval: int
	tupltype: str
	body: bytes


def parse(data: bytes) -> PAM:
	assert data.startswith(b"P7\n"), data[:16]
	hdr: dict[str, str] = {}
	pos = 3
	while True:
		nl = data.index(b"\n", pos)
		line = data[pos:nl].decode("latin-1")  # ImageMagick can put non-ASCII in a header comment
		pos = nl + 1
		if line == "ENDHDR":
			break
		if not line.strip() or line.startswith("#"):
			continue
		key, _, value = line.partition(" ")
		hdr[key] = value.strip()
	w, h, d, maxval = (int(hdr[k]) for k in ("WIDTH", "HEIGHT", "DEPTH", "MAXVAL"))
	assert 1 <= d <= 4, d
	assert 1 <= maxval <= 65535, maxval
	body = data[pos:]
	bps = 2 if maxval > 255 else 1
	expected = w * h * d * bps
	tt = hdr.get("TUPLTYPE", "")
	if tt == "BLACKANDWHITE" and maxval == 1 and d == 1:
		# GraphicsMagick writes BLACKANDWHITE as PBM data under a PAM header: packed rows
		# and PBM polarity (1 = black), where PAM defines 1 = white. Unpack and invert.
		# Keyed on the tuple type: at width 1 the packed and unpacked sizes coincide.
		rowbytes = (w + 7) // 8
		assert len(body) == rowbytes * h, f"BLACKANDWHITE body {len(body)} bytes for {w}x{h} packed"
		out = bytearray(w * h)
		for y in range(h):
			row = body[y * rowbytes:(y + 1) * rowbytes]
			for x in range(w):
				out[y * w + x] = 1 - ((row[x >> 3] >> (7 - (x & 7))) & 1)
		body = bytes(out)
	assert len(body) == expected, f"PAM body {len(body)} bytes, expected {expected} ({w}x{h}x{d}, maxval {maxval})"
	return PAM(w, h, d, maxval, tt, body)


def rgba16(p: PAM) -> np.ndarray:
	"""RGBA, 16 bits per sample, straight alpha, no color management: the form every output is compared in.

	Every PNG maxval (1, 3, 15, 255, 65535) divides 65535, so those scale exactly. Other maxvals
	(pypng rescales to the sBIT depth) round to nearest and then differ, which is the truth about them.
	"""
	a = np.frombuffer(p.body, dtype=">u2" if p.maxval > 255 else "u1").reshape(p.height, p.width, p.depth).astype(np.uint32)
	if 65535 % p.maxval == 0:
		a = a * (65535 // p.maxval)
	else:
		a = (a * 65535 + p.maxval // 2) // p.maxval
	opaque = np.full(a.shape[:2] + (1,), 65535, dtype=np.uint32)
	match p.depth:
		case 1:
			rgba = np.concatenate([a, a, a, opaque], axis=2)
		case 2:
			rgba = np.concatenate([a[:, :, :1]] * 3 + [a[:, :, 1:2]], axis=2)
		case 3:
			rgba = np.concatenate([a, opaque], axis=2)
		case _:
			rgba = a
	assert rgba.shape == (p.height, p.width, 4), rgba.shape
	return rgba.astype(np.uint16)


def digest(rgba: np.ndarray) -> str:
	h = hashlib.blake2b(digest_size=12)
	h.update(f"{rgba.shape[1]}x{rgba.shape[0]}\n".encode())
	h.update(np.ascontiguousarray(rgba).tobytes())
	return h.hexdigest()
