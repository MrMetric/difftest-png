"""A DEFLATE (RFC 1951) and zlib (RFC 1950) writer by hand, for stream shapes zlib never emits.

Blocks are written token by token into a BitWriter: `stored_block`, `fixed_block` and `dynamic_block`,
whose code lengths can be given explicitly to shape the trees (a single distance code, no distance code,
unused codes for distance symbols 30 and 31). A token is ("lit", byte), ("match", length, distance), or,
in a dynamic block, ("raw", length symbol, extra, extra bits, distance symbol, extra, extra bits) to
write any code at all. `zlib_wrap` frames a raw stream with any header.

This is the encoder's mirror of inflate.py and shares nothing with it, so that a mistake in one is not
cancelled by the same mistake in the other.
"""
import heapq
import struct
import zlib


class BitWriter:
	"""DEFLATE bit order: values LSB first; Huffman codes MSB first (RFC 1951 3.1.1)."""

	def __init__(self):
		self.out = bytearray()
		self.acc = 0
		self.n = 0

	def bits(self, value: int, count: int) -> None:
		assert 0 <= value < (1 << count), (value, count)
		self.acc |= value << self.n
		self.n += count
		while self.n >= 8:
			self.out.append(self.acc & 0xFF)
			self.acc >>= 8
			self.n -= 8

	def code(self, code: int, length: int) -> None:
		rev = int(f"{code:0{length}b}"[::-1], 2) if length else 0
		self.bits(rev, length)

	def align(self) -> None:
		if self.n:
			self.out.append(self.acc & 0xFF)
			self.acc = self.n = 0

	def bytes(self) -> bytes:
		self.align()
		return bytes(self.out)


def huffman_lengths(freqs: dict[int, int], max_len: int) -> dict[int, int]:
	"""Code lengths for the symbols with nonzero frequency (plain Huffman; asserts the length limit)."""
	syms = [s for s, f in freqs.items() if f > 0]
	if not syms:
		return {}
	if len(syms) == 1:
		return {syms[0]: 1}
	heap = [(freqs[s], i, [s]) for i, s in enumerate(syms)]
	heapq.heapify(heap)
	lengths = {s: 0 for s in syms}
	tick = len(syms)
	while len(heap) > 1:
		f1, _, s1 = heapq.heappop(heap)
		f2, _, s2 = heapq.heappop(heap)
		for s in s1 + s2:
			lengths[s] += 1
		heapq.heappush(heap, (f1 + f2, tick, s1 + s2))
		tick += 1
	assert max(lengths.values()) <= max_len, f"Huffman length {max(lengths.values())} exceeds {max_len}; use flatter data"
	return lengths


def canonical_codes(lengths: dict[int, int]) -> dict[int, tuple[int, int]]:
	"""RFC 1951 3.2.2: symbol -> (code, length)."""
	bl_count: dict[int, int] = {}
	for l in lengths.values():
		if l:
			bl_count[l] = bl_count.get(l, 0) + 1
	code = 0
	next_code = {}
	for b in range(1, max(bl_count, default=0) + 1):
		code = (code + bl_count.get(b - 1, 0)) << 1
		next_code[b] = code
	out = {}
	for sym in sorted(lengths):
		l = lengths[sym]
		if l:
			out[sym] = (next_code[l], l)
			next_code[l] += 1
	return out


# RFC 1951 3.2.5
LENGTH_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258]
LENGTH_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0]
DIST_BASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577]
DIST_EXTRA = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13]
# RFC 1951 3.2.7: the order in which code length code lengths are written
CL_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]
# RFC 1951 3.2.6
FIXED_LIT = {s: 8 for s in range(144)} | {s: 9 for s in range(144, 256)} | {s: 7 for s in range(256, 280)} | {s: 8 for s in range(280, 288)}


def length_symbol(length: int) -> tuple[int, int, int]:
	"""(symbol, extra value, extra bits) for a match length 3..258."""
	assert 3 <= length <= 258, length
	for i in range(len(LENGTH_BASE) - 1, -1, -1):
		if length >= LENGTH_BASE[i]:
			return 257 + i, length - LENGTH_BASE[i], LENGTH_EXTRA[i]
	raise AssertionError(length)


def dist_symbol(dist: int) -> tuple[int, int, int]:
	"""(symbol, extra value, extra bits) for a match distance 1..32768."""
	assert 1 <= dist <= 32768, dist
	for i in range(len(DIST_BASE) - 1, -1, -1):
		if dist >= DIST_BASE[i]:
			return i, dist - DIST_BASE[i], DIST_EXTRA[i]
	raise AssertionError(dist)


def tokenize_runs(data: bytes, min_run: int = 3) -> list[tuple]:
	"""Literals plus distance-1 matches for runs of a repeated byte (the only match an encoder needs to make a one-code distance tree)."""
	out, i = [], 0
	while i < len(data):
		j = i + 1
		while j < len(data) and data[j] == data[i] and j - i < 259:
			j += 1
		run = j - i
		if run - 1 >= min_run:  # a literal, then a distance-1 match of at least min_run bytes
			out.append(("lit", data[i]))
			out.append(("match", min(run - 1, 258), 1))
			i += 1 + min(run - 1, 258)
		else:
			out.append(("lit", data[i]))
			i += 1
	return out


def detokenize(tokens: list[tuple]) -> bytes:
	"""The bytes that ("lit", ...) and ("match", ...) tokens stand for."""
	out = bytearray()
	for t in tokens:
		if t[0] == "lit":
			out.append(t[1])
		else:
			assert t[0] == "match" and 1 <= t[2] <= len(out), t
			for _ in range(t[1]):
				out.append(out[-t[2]])
	return bytes(out)


def dynamic_block(w: BitWriter, tokens: list[tuple], final: bool, lit_lengths: dict[int, int] | None = None,
		dist_lengths: dict[int, int] | None = None, hdist_min: int = 1) -> None:
	"""Write one dynamic-Huffman block (RFC 1951 3.2.7) for `tokens`.

	Code lengths default to Huffman over the token statistics; pass explicit dictionaries to shape the
	trees (for example a single distance code, or unused codes for distance symbols 30 and 31).
	"""
	lit_freq: dict[int, int] = {256: 1}
	dist_freq: dict[int, int] = {}
	for t in tokens:
		if t[0] == "lit":
			lit_freq[t[1]] = lit_freq.get(t[1], 0) + 1
		elif t[0] == "raw":
			lit_freq[t[1]] = lit_freq.get(t[1], 0) + 1
			dist_freq[t[4]] = dist_freq.get(t[4], 0) + 1
		else:
			ls = length_symbol(t[1])[0]
			ds = dist_symbol(t[2])[0]
			lit_freq[ls] = lit_freq.get(ls, 0) + 1
			dist_freq[ds] = dist_freq.get(ds, 0) + 1
	if lit_lengths is None:
		lit_lengths = huffman_lengths(lit_freq, 15)
	if dist_lengths is None:
		dist_lengths = huffman_lengths(dist_freq, 15)
	lit_codes = canonical_codes(lit_lengths)
	dist_codes = canonical_codes(dist_lengths)
	hlit = max(max(lit_lengths, default=256) + 1, 257)
	hdist = max(max(dist_lengths, default=-1) + 1, hdist_min)
	assert hlit <= 288 and hdist <= 32, (hlit, hdist)
	cl_seq = [lit_lengths.get(s, 0) for s in range(hlit)] + [dist_lengths.get(s, 0) for s in range(hdist)]
	cl_freq: dict[int, int] = {}
	for l in cl_seq:
		cl_freq[l] = cl_freq.get(l, 0) + 1
	cl_lengths = huffman_lengths(cl_freq, 7)
	if len(cl_lengths) == 1:  # a single code-length code: give it a complete two-code tree with a dummy partner
		only = next(iter(cl_lengths))
		cl_lengths = {only: 1, (only + 1) % 16 if only != 15 else 0: 1}
	cl_codes = canonical_codes(cl_lengths)
	hclen = max(i for i, s in enumerate(CL_ORDER) if s in cl_lengths) + 1
	hclen = max(hclen, 4)
	w.bits(1 if final else 0, 1)
	w.bits(2, 2)
	w.bits(hlit - 257, 5)
	w.bits(hdist - 1, 5)
	w.bits(hclen - 4, 4)
	for s in CL_ORDER[:hclen]:
		w.bits(cl_lengths.get(s, 0), 3)
	for l in cl_seq:
		w.code(*cl_codes[l])
	for t in tokens:
		if t[0] == "lit":
			w.code(*lit_codes[t[1]])
		elif t[0] == "raw":
			_, ls, extra, nbits, ds, dextra, dbits = t
			w.code(*lit_codes[ls])
			w.bits(extra, nbits)
			w.code(*dist_codes[ds])
			w.bits(dextra, dbits)
		else:
			ls, extra, nbits = length_symbol(t[1])
			w.code(*lit_codes[ls])
			w.bits(extra, nbits)
			ds, dextra, dbits = dist_symbol(t[2])
			w.code(*dist_codes[ds])
			w.bits(dextra, dbits)
	w.code(*lit_codes[256])


def fixed_block(w: BitWriter, tokens: list[tuple], final: bool) -> None:
	"""Fixed-Huffman block (RFC 1951 3.2.6)."""
	lit_codes = canonical_codes(FIXED_LIT)
	w.bits(1 if final else 0, 1)
	w.bits(1, 2)
	for t in tokens:
		if t[0] == "lit":
			w.code(*lit_codes[t[1]])
		else:
			ls, extra, nbits = length_symbol(t[1])
			w.code(*lit_codes[ls])
			w.bits(extra, nbits)
			ds, dextra, dbits = dist_symbol(t[2])
			w.code(ds, 5)
			w.bits(dextra, dbits)
	w.code(*lit_codes[256])


def stored_block(w: BitWriter, data: bytes, final: bool, nlen: int | None = None) -> None:
	"""Stored block (RFC 1951 3.2.4); `nlen` overrides the one's complement of LEN."""
	assert len(data) <= 65535
	w.bits(1 if final else 0, 1)
	w.bits(0, 2)
	w.align()
	w.out += struct.pack("<HH", len(data), (~len(data) & 0xFFFF) if nlen is None else nlen)
	w.out += data


def zlib_wrap(deflate: bytes, raw: bytes, *, cmf: int = 0x78, flevel: int = 2, fdict: bytes | None = None, adler: int | None = None) -> bytes:
	"""zlib framing (RFC 1950) around a raw DEFLATE stream: CMF, FLG with a correct FCHECK, the optional
	dictionary id, and the Adler-32 of `raw` unless `adler` overrides it."""
	flg = (flevel << 6) | (0x20 if fdict is not None else 0)
	flg |= (31 - (cmf * 256 + flg) % 31) % 31
	head = bytes([cmf, flg]) + (fdict or b"")
	return head + deflate + struct.pack(">I", zlib.adler32(raw) & 0xFFFFFFFF if adler is None else adler)


def deflate_hand(raw: bytes, kind: str = "dynamic", final: bool = True, **kw) -> bytes:
	"""Raw DEFLATE of `raw` as one block of the given kind: dynamic | fixed | stored | runs (dynamic with distance-1 matches)."""
	w = BitWriter()
	if kind == "stored":
		stored_block(w, raw, final, **kw)
	elif kind == "fixed":
		fixed_block(w, tokenize_runs(raw), final)
	elif kind == "runs":
		dynamic_block(w, tokenize_runs(raw), final, **kw)
	else:
		assert kind == "dynamic", kind
		dynamic_block(w, [("lit", b) for b in raw], final, **kw)
	return w.bytes()
