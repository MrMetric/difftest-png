"""PNG encoder primitives: build datastreams from parts, one specification stage per function.

Packing (7.2), filtering (9), Adam7 pass extraction (8.1), compression and chunking are separate
functions with their own arguments, so a test file can be exactly one step away from a conforming one.
`chunk` takes an explicit length or CRC to build corrupt chunks; `build` assembles a whole datastream.
Encoder side only: nothing here reads PNG.
"""
import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Adam7 passes: x start, y start, x step, y step (8.1).
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
FILTERS = ("none", "sub", "up", "average", "paeth")
STATS = ("wrap", "avg_odd", "avg_carry", "paeth_a", "paeth_b", "paeth_c", "paeth_tie")


# ---------------------------------------------------------------- chunks

def crc(ctype: bytes, data: bytes) -> int:
	return zlib.crc32(ctype + data) & 0xFFFFFFFF


def chunk(ctype: bytes, data: bytes = b"", *, crc_value: int | None = None, length: int | None = None) -> bytes:
	"""One chunk. `crc_value` and `length` override the correct values to build corrupt chunks."""
	assert len(ctype) == 4, ctype
	return (struct.pack(">I", len(data) if length is None else length) + ctype + data
		+ struct.pack(">I", crc(ctype, data) if crc_value is None else crc_value))


def assemble(chunks: list[bytes], signature: bytes = SIGNATURE) -> bytes:
	return signature + b"".join(chunks)


def ihdr(width: int, height: int, bit_depth: int, color_type: int, interlace: int = 0, compression: int = 0, filter_method: int = 0) -> bytes:
	return chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, compression, filter_method, interlace))


# ---------------------------------------------------------------- pixels -> filtered scanlines

def pass_size(width: int, height: int, p: int) -> tuple[int, int]:
	"""Width and height of Adam7 reduced image p (0..6); either may be 0."""
	xs, ys, xstep, ystep = ADAM7[p]
	pw = (width - xs + xstep - 1) // xstep if width > xs else 0
	ph = (height - ys + ystep - 1) // ystep if height > ys else 0
	return pw, ph


def row_bytes(width: int, color_type: int, bit_depth: int) -> int:
	return (width * CHANNELS[color_type] * bit_depth + 7) // 8


def bpp(color_type: int, bit_depth: int) -> int:
	"""Filter byte distance (9.2): bytes per complete pixel, rounded up to one."""
	return max(1, CHANNELS[color_type] * bit_depth // 8)


def pack(samples: list[int], bit_depth: int, pad_bits: int = 0) -> bytes:
	"""Pack one scanline, leftmost sample in the high-order bits (7.2). `pad_bits` = 1 sets the unused low-order bits."""
	if bit_depth == 16:
		return b"".join(struct.pack(">H", s) for s in samples)
	if bit_depth == 8:
		return bytes(samples)
	out, acc, n = bytearray(), 0, 0
	for s in samples:
		assert 0 <= s < (1 << bit_depth), (s, bit_depth)
		acc = (acc << bit_depth) | s
		n += bit_depth
		if n == 8:
			out.append(acc)
			acc = n = 0
	if n:
		fill = ((1 << (8 - n)) - 1) if pad_bits else 0
		out.append((acc << (8 - n)) | fill)
	return bytes(out)


def paeth(a: int, b: int, c: int) -> int:
	"""9.4, including the tie order a, b, c."""
	p = a + b - c
	pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
	if pa <= pb and pa <= pc:
		return a
	if pb <= pc:
		return b
	return c


def filt(ftype: int, cur: bytes, prev: bytes, dist: int, stats: dict[str, int] | None = None) -> bytes:
	"""Apply filter type `ftype` to one scanline given the previous (unfiltered) scanline.

	`stats`, when given, counts per byte the cases where a decoder's arithmetic shows (keys STATS):
	`wrap`: Sub or Up, the byte minus its prediction is negative (the subtraction wraps modulo 256);
	`avg_odd`: Average, a + b is odd (the halving truncates); `avg_carry`: Average, a + b >= 256 (the
	sum needs nine bits); `paeth_a`/`paeth_b`/`paeth_c`: which predictor Paeth chose; `paeth_tie`:
	Paeth's chosen distance equals another's whose predictor has a different value, so the tie order
	decides the result.
	"""
	assert len(cur) == len(prev), (len(cur), len(prev))
	out = bytearray(len(cur))
	for i, x in enumerate(cur):
		a = cur[i - dist] if i >= dist else 0
		b = prev[i]
		c = prev[i - dist] if i >= dist else 0
		match ftype:
			case 0:
				pred = 0
			case 1:
				pred = a
			case 2:
				pred = b
			case 3:
				pred = (a + b) // 2
			case 4:
				pred = paeth(a, b, c)
			case _:
				raise AssertionError(ftype)
		if stats is not None:
			_count(stats, ftype, x, pred, a, b, c)
		out[i] = (x - pred) & 0xFF
	return bytes(out)


def _count(stats: dict[str, int], ftype: int, x: int, pred: int, a: int, b: int, c: int) -> None:
	def bump(key: str) -> None:
		stats[key] = stats.get(key, 0) + 1
	if ftype in (1, 2) and x - pred < 0:
		bump("wrap")
	elif ftype == 3:
		if (a + b) % 2:
			bump("avg_odd")
		if a + b >= 256:
			bump("avg_carry")
	elif ftype == 4:
		p = a + b - c
		pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
		if pa <= pb and pa <= pc:
			bump("paeth_a")
			tie = (pa == pb and a != b) or (pa == pc and a != c)
		elif pb <= pc:
			bump("paeth_b")
			tie = pb == pc and b != c
		else:
			bump("paeth_c")
			tie = False
		if tie:
			bump("paeth_tie")


def extract_pass(rows: list[list[int]], width: int, color_type: int, p: int) -> list[list[int]]:
	"""Reduced image p (0..6) of an Adam7 image given full rows of samples."""
	xs, ys, xstep, ystep = ADAM7[p]
	nch = CHANNELS[color_type]
	out = []
	for y in range(ys, len(rows), ystep):
		row = rows[y]
		out.append([row[x * nch + c] for x in range(xs, width, xstep) for c in range(nch)])
	return out


def serialize(rows, width: int, height: int, color_type: int, bit_depth: int, interlace: int = 0,
		filter_for=lambda p, y: 0, pad_bits: int = 0, stats: dict[str, int] | None = None) -> bytes:
	"""Pixel rows -> the byte sequence that is compressed (filter byte + filtered scanline per non-empty scanline).

	`filter_for(pass_index, row_index_within_pass)` chooses the filter type; pass_index is 0 for a
	non-interlaced image. Empty passes contribute nothing (9.1). `stats` is passed to `filt`.
	"""
	assert len(rows) == height and all(len(r) == width * CHANNELS[color_type] for r in rows), "rows do not match the IHDR"
	dist = bpp(color_type, bit_depth)
	out = bytearray()
	passes = [(p, extract_pass(rows, width, color_type, p)) for p in range(7)] if interlace else [(0, rows)]
	for p, prows in passes:
		pw = pass_size(width, height, p)[0] if interlace else width
		if pw == 0 or not prows:
			continue
		prev = bytes(row_bytes(pw, color_type, bit_depth))
		for y, r in enumerate(prows):
			cur = pack(r, bit_depth, pad_bits)
			f = filter_for(p, y)
			out.append(f)
			out += filt(f, cur, prev, dist, stats)
			prev = cur
	return bytes(out)


# ---------------------------------------------------------------- whole files

def build(width: int, height: int, bit_depth: int, color_type: int, rows, *, interlace: int = 0,
		filter_for=lambda p, y: 0, pad_bits: int = 0, plte: bytes | None = None, trns: bytes | None = None,
		before_plte=(), after_plte=(), after_idat=(), compress=None, idat_sizes: list[int] | None = None,
		iend: bytes | None = None, stats: dict[str, int] | None = None) -> bytes:
	"""A complete datastream. `compress(raw) -> zlib bytes` defaults to zlib level 6; `idat_sizes` splits the stream."""
	raw = serialize(rows, width, height, color_type, bit_depth, interlace, filter_for, pad_bits, stats)
	z = (compress or (lambda r: zlib.compress(r, 6)))(raw)
	parts = [ihdr(width, height, bit_depth, color_type, interlace)] + list(before_plte)
	if plte is not None:
		parts.append(chunk(b"PLTE", plte))
	if trns is not None:
		parts.append(chunk(b"tRNS", trns))
	parts += list(after_plte)
	if idat_sizes:
		pos = 0
		for n in idat_sizes:
			parts.append(chunk(b"IDAT", z[pos:pos + n]))
			pos += n
		if pos < len(z):
			parts.append(chunk(b"IDAT", z[pos:]))
	else:
		parts.append(chunk(b"IDAT", z))
	parts += list(after_idat)
	parts.append(chunk(b"IEND") if iend is None else iend)
	return assemble(parts)
