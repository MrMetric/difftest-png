"""RFC 1950 (zlib) and RFC 1951 (DEFLATE) decoder with every rule named, plus Adler-32.

Each check carries an id from spec/clauses.tsv (`rfc1951-3.2.7#hlit`) and quotes the RFC
sentence it enforces. A violation raises `InflateError(id, message, output)`; input that ends
before the stream does raises its subclass `Truncated`, whose `output` holds every byte decoded
so far, so the caller can decide what a short stream means for it. Every rule a stream
exercises (block kinds, HDIST above 30, a distance crossing a block boundary, ...) is added to
the caller's diagnostic record. Coverage-only events cannot name fault clauses.

Where the RFC is silent or ambiguous the policy is stated at the check:
- incomplete code-length sets are rejected except the two the RFC describes (one distance code
  of one bit, or no distance codes at all) and a lone literal/length code of one bit, which zlib
  (inftrees.c) and puff.c accept and which the caller records;
- HDIST 31 or 32 is accepted (the header range is 1-32 and 3.3 requires the full range; zlib refuses it);
  distance codes 30 and 31 are rejected when they occur in the data;
- length code 284 with extra bits 31 (length 258, outside the table's 227-257) is rejected;
- a distance beyond the window CINFO declares, but within 32768, is accepted and recorded.
"""
from itertools import accumulate

from pngdiagnostics import StreamDiagnostics

# RFC 1951 3.2.5: base value and number of extra bits for length codes 257..285 and distance codes 0..29.
LENGTH_BASE = (3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258)
LENGTH_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0)
DIST_BASE = (1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097,
	6145, 8193, 12289, 16385, 24577)
DIST_EXTRA = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13)
# RFC 1951 3.2.7: "in the order: 16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15"
CL_ORDER = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)
# RFC 1951 3.2.6: fixed literal/length code lengths, and 5-bit codes for distance codes 0-31.
FIXED_LIT = (8,) * 144 + (9,) * 112 + (7,) * 24 + (8,) * 8
FIXED_DIST = (5,) * 32


class InflateError(Exception):
	"""The stream breaks the rule `id`; `output` holds the bytes decoded before the fault."""

	def __init__(self, id: str, msg: str, output: bytes | bytearray = b""):
		super().__init__(msg)
		self.id = id
		self.output = bytes(output)


class Truncated(InflateError):
	"""The input ended before the stream did; `output` holds everything decoded."""


# ---------------------------------------------------------------- RFC 1950

def adler32(data: bytes) -> int:
	"""RFC 1950 2.2: "s1 is the sum of all bytes, s2 is the sum of all s1 values. Both sums are done
	modulo 65521. s1 is initialized to 1, s2 to zero. The Adler-32 checksum is stored as s2*65536 + s1".

	Per block of n bytes s1 grows by sum(block) and s2 by n*s1 plus the sum of the block's prefix sums.
	"""
	s1, s2 = 1, 0
	for i in range(0, len(data), 1 << 16):
		block = data[i:i + (1 << 16)]
		s2 = (s2 + len(block) * s1 + sum(accumulate(block))) % 65521
		s1 = (s1 + sum(block)) % 65521
	return (s2 << 16) | s1


def zlib_decompress(data: bytes, report: StreamDiagnostics, *, observer=None) -> tuple[bytes, int]:
	"""Decode and record every detected condition before returning or raising."""
	assert isinstance(report, StreamDiagnostics), "inflate requires the shared diagnostic sink"
	try:
		try:
			return _zlib_decompress(data, report, observer)
		finally:
			if observer is not None:
				observer.finish()
	except InflateError as e:
		report.failure(e)
		raise


def inflate(data: bytes, report: StreamDiagnostics, pos: int = 0, window: int = 32768) -> tuple[bytes, int]:
	"""Raw DEFLATE with the same mandatory recording boundary as zlib."""
	assert isinstance(report, StreamDiagnostics), "inflate requires the shared diagnostic sink"
	try:
		return _inflate(data, pos, report, window)
	except InflateError as e:
		report.failure(e)
		raise


def _zlib_decompress(data: bytes, report: StreamDiagnostics, observer=None) -> tuple[bytes, int]:
	"""Decode the zlib stream at the start of `data`. Returns (output, end): data[end:] is not part of it.

	RFC 1950 2.2: "Any data which may appear after ADLER32 are not part of the zlib stream."
	"""
	if len(data) < 2:
		raise Truncated("rfc1950-2.2#end-in-header", f"{len(data)} bytes of zlib stream; the header alone is 2")
	cmf, flg = data[0], data[1]
	# 2.2 "CM = 8 denotes the "deflate" compression method"; 2.3 "A compliant decompressor must give an
	# error indication if CM is not one of the values defined in this specification (only the value 8 is permitted"
	if cmf & 15 != 8:
		raise InflateError("rfc1950-2.2#cm", f"compression method {cmf & 15}, only 8 is defined")
	# 2.2 "Values of CINFO above 7 are not allowed in this version of the specification."
	cinfo = cmf >> 4
	if cinfo > 7:
		raise InflateError("rfc1950-2.2#cinfo", f"CINFO {cinfo}: window 2^{cinfo + 8} exceeds 32768")
	if cinfo < 7:
		report.reach("rfc1950-2.2#cinfo-small")
	# 2.2 "The FCHECK value must be such that CMF and FLG, when viewed as a 16-bit unsigned integer stored
	# in MSB order (CMF*256 + FLG), is a multiple of 31."
	if (cmf * 256 + flg) % 31:
		raise InflateError("rfc1950-2.2#fcheck", f"CMF*256+FLG = {cmf * 256 + flg} is not a multiple of 31")
	# 2.3 "When the other format does not use the preset dictionary feature, a compliant decompressor must
	# reject any stream in which the FDICT flag is set." (Every caller here is PNG, which has no dictionaries.)
	if flg & 0x20:
		raise InflateError("rfc1950-2.3#fdict", "FDICT set; no preset dictionary is defined for this format")
	out, end = _inflate(data, 2, report, window=1 << (cinfo + 8), observer=observer)
	# 2.2 ADLER32 "contains a checksum value of the uncompressed data"; 2.3 "A compliant decompressor must
	# check CMF, FLG, and ADLER32, and provide an error indication if any of these have incorrect values."
	if len(data) < end + 4:
		raise Truncated("rfc1950-2.2#end-in-adler32", f"Adler-32 missing ({len(data) - end} of 4 bytes present)", out)
	stored = int.from_bytes(data[end:end + 4], "big")
	if stored != adler32(out):
		raise InflateError("rfc1950-2.3#adler32", f"Adler-32 {stored:08x}, data gives {adler32(out):08x}", out)
	return out, end + 4


# ---------------------------------------------------------------- RFC 1951

class _Bits:
	"""3.1.1: "Data elements are packed into bytes in order of increasing bit number within the byte, i.e.,
	starting with the least-significant bit of the byte." `buf` holds `cnt` not yet consumed bits, LSB first."""

	def __init__(self, data: bytes, pos: int):
		self.data, self.pos, self.buf, self.cnt = data, pos, 0, 0

	def need(self, n: int, out: bytearray) -> None:
		while self.cnt < n:
			if self.pos >= len(self.data):
				raise Truncated("rfc1951-3.2.3#end-of-input", "input ends inside the deflate stream", out)
			self.buf |= self.data[self.pos] << self.cnt
			self.pos += 1
			self.cnt += 8

	def bits(self, n: int, out: bytearray) -> int:
		"""3.1.1: "Data elements other than Huffman codes are packed starting with the least-significant bit"."""
		self.need(n, out)
		v = self.buf & ((1 << n) - 1)
		self.buf >>= n
		self.cnt -= n
		return v

	def align(self) -> int:
		"""Drop the bits up to the next byte boundary; return the index of the next unread byte."""
		self.pos -= self.cnt >> 3
		self.buf = self.cnt = 0
		return self.pos


def _code(lengths: list[int] | tuple[int, ...]) -> tuple[list[int], int, int]:
	"""3.2.2: the canonical Huffman code given by `lengths`. Returns (table, maxbits, left).

	table[k], for k the next maxbits input bits, is symbol << 4 | code length, or -1 where no code
	starts with those bits. 3.1.1: "Huffman codes are packed starting with the most-significant bit of
	the code", so each code is entered bit-reversed. `left` is the unused code space in units of
	2^-15: 0 for a complete code; the caller decides what an incomplete code means.
	"""
	bl_count = [0] * 16
	for n in lengths:
		bl_count[n] += 1
	bl_count[0] = 0
	left = 1 << 15
	for n in range(1, 16):
		left -= bl_count[n] << (15 - n)
		# Lengths with more codes than the bit width can hold define no prefix code at all.
		if left < 0:
			raise InflateError("rfc1951-3.2.2#over-subscribed", f"code lengths over-subscribed at length {n}")
	maxbits = max(lengths, default=0)
	# 3.2.2 step 2: "code = (code + bl_count[bits-1]) << 1; next_code[bits] = code;"
	next_code, code = [0] * 16, 0
	for n in range(1, 16):
		code = (code + bl_count[n - 1]) << 1
		next_code[n] = code
	table = [-1] * (1 << maxbits)
	# 3.2.2 step 3: "Assign numerical values to all codes, using consecutive values for all codes of the same
	# length ... Codes that are never used (which have a bit length of zero) must not be assigned a value."
	for sym, n in enumerate(lengths):
		if n:
			rev = int(format(next_code[n], f"0{n}b")[::-1], 2)
			next_code[n] += 1
			for k in range(rev, 1 << maxbits, 1 << n):
				table[k] = sym << 4 | n
	return table, maxbits, left


def _decode(b: _Bits, table: list[int], maxbits: int, out: bytearray, what: str) -> int:
	"""One Huffman-coded symbol."""
	while b.cnt < maxbits and b.pos < len(b.data):
		b.buf |= b.data[b.pos] << b.cnt
		b.pos += 1
		b.cnt += 8
	e = table[b.buf & ((1 << maxbits) - 1)]
	if e < 0 or e & 15 > b.cnt:
		if b.cnt < maxbits:
			raise Truncated("rfc1951-3.2.3#end-of-input", f"input ends inside a {what} code", out)
		raise InflateError("rfc1951-3.2.2#unassigned-code", f"bits {b.buf & ((1 << maxbits) - 1):0{maxbits}b} are not a {what} code", out)
	b.buf >>= e & 15
	b.cnt -= e & 15
	return e >> 4


def _inflate(data: bytes, pos: int, report: StreamDiagnostics, window: int = 32768, observer=None) -> tuple[bytes, int]:
	"""Decode the raw DEFLATE stream at data[pos:]. Returns (output, end), `end` the index of the first byte
	after the final block. `window` is the LZ77 window the container declares (only recorded, see below)."""
	out = bytearray()
	try:
		return _blocks(_Bits(data, pos), out, report, window, observer)
	except InflateError as e:
		# Every failure, including a code-table construction failure, carries the complete output prefix.
		e.output = bytes(out)
		raise


def _blocks(b: _Bits, out: bytearray, report: StreamDiagnostics, window: int, observer=None) -> tuple[bytes, int]:
	blocks = 0
	while True:
		# 3.2.3 "Each block of compressed data begins with 3 header bits containing the following data:
		# first bit BFINAL, next 2 bits BTYPE"
		if observer is not None:
			# Buffered lookahead has not been consumed; restart positions use the logical bit cursor.
			observer.block(b.pos * 8 - b.cnt, len(out))
		final = b.bits(1, out)
		btype = b.bits(2, out)
		start = len(out)
		blocks += 1
		if blocks == 2:
			report.reach("rfc1951-3.2.3#multiple-blocks")
		if btype == 0:
			_stored(b, out, report)
		elif btype == 1:
			report.reach("rfc1951-3.2.6#fixed")
			lit, litbits, _ = _code(FIXED_LIT)
			dist, distbits, _ = _code(FIXED_DIST)
			_huffman(b, out, lit, litbits, dist, distbits, start, window, report, observer)
		elif btype == 2:
			report.reach("rfc1951-3.2.7#dynamic")
			lit, litbits, dist, distbits = _dynamic_header(b, out, report)
			_huffman(b, out, lit, litbits, dist, distbits, start, window, report, observer)
		else:
			# 3.2.3 "11 - reserved (error)"
			raise InflateError("rfc1951-3.2.3#btype-reserved", "block type 3 is reserved", out)
		if len(out) == start:
			report.reach("rfc1951-3.2.3#empty-block")
		# 3.2.3 "BFINAL is set if and only if this is the last block of the data set."
		if final:
			return bytes(out), b.align()


def _stored(b: _Bits, out: bytearray, report: StreamDiagnostics) -> None:
	"""3.2.4 Non-compressed blocks (BTYPE=00)."""
	report.reach("rfc1951-3.2.4#stored")
	# "Any bits of input up to the next byte boundary are ignored."
	pos = b.align()
	data = b.data
	if pos + 4 > len(data):
		raise Truncated("rfc1951-3.2.3#end-of-input", "input ends inside a stored block header", out)
	n = int.from_bytes(data[pos:pos + 2], "little")
	nlen = int.from_bytes(data[pos + 2:pos + 4], "little")
	# "LEN is the number of data bytes in the block. NLEN is the one's complement of LEN."
	if nlen != n ^ 0xFFFF:
		raise InflateError("rfc1951-3.2.4#nlen", f"stored block LEN {n:04x}, NLEN {nlen:04x} is not its complement", out)
	out += data[pos + 4:pos + 4 + n]
	if pos + 4 + n > len(data):
		raise Truncated("rfc1951-3.2.3#end-of-input", f"input ends inside a {n}-byte stored block", out)
	b.pos = pos + 4 + n


def _dynamic_header(b: _Bits, out: bytearray, report: StreamDiagnostics) -> tuple[list[int], int, list[int], int]:
	"""3.2.7: read the code-length code, then the literal/length and distance code lengths."""
	# "5 Bits: HLIT, # of Literal/Length codes - 257 (257 - 286)"
	hlit = b.bits(5, out) + 257
	if hlit > 286:
		raise InflateError("rfc1951-3.2.7#hlit", f"HLIT gives {hlit} literal/length codes; the range is 257-286", out)
	# "5 Bits: HDIST, # of Distance codes - 1 (1 - 32)": every 5-bit value is in range, and 3.3 "A compliant
	# decompressor must accept the full range of possible values defined in the previous section". Codes 30 and
	# 31 then have lengths but "will never actually occur in the compressed data" (3.2.6); see _huffman.
	hdist = b.bits(5, out) + 1
	if hdist > 30:
		report.reach("rfc1951-3.2.7#hdist-over-30")
	# "4 Bits: HCLEN, # of Code Length codes - 4 (4 - 19)"
	hclen = b.bits(4, out) + 4
	cl_lengths = [0] * 19
	for sym in CL_ORDER[:hclen]:
		cl_lengths[sym] = b.bits(3, out)
	cl, clbits, left = _code(cl_lengths)
	# The code-length code is a Huffman code; the only incomplete codes the RFC describes are for distances.
	if left:
		raise InflateError("rfc1951-3.2.2#incomplete", "the code-length code is incomplete", out)
	# "The code length repeat codes can cross from HLIT + 257 to the HDIST + 1 code lengths. In other words,
	# all code lengths form a single sequence of HLIT + HDIST + 258 values."
	lengths: list[int] = []
	while len(lengths) < hlit + hdist:
		sym = _decode(b, cl, clbits, out, "code-length")
		if sym < 16:
			lengths.append(sym)
			continue
		if sym == 16:
			# "16: Copy the previous code length 3 - 6 times."
			if not lengths:
				raise InflateError("rfc1951-3.2.7#repeat-without-previous", "code length 16 with no previous length", out)
			value, count = lengths[-1], 3 + b.bits(2, out)
		elif sym == 17:
			# "17: Repeat a code length of 0 for 3 - 10 times."
			value, count = 0, 3 + b.bits(3, out)
		else:
			# "18: Repeat a code length of 0 for 11 - 138 times"
			value, count = 0, 11 + b.bits(7, out)
		if len(lengths) + count > hlit + hdist:
			raise InflateError("rfc1951-3.2.7#repeat-overflow",
				f"a repeat of {count} runs past the {hlit + hdist} code lengths", out)
		if len(lengths) < hlit < len(lengths) + count:
			report.reach("rfc1951-3.2.7#repeat-crosses")
		lengths += [value] * count
	lit_lengths, dist_lengths = lengths[:hlit], lengths[hlit:]
	# "The literal/length symbol 256 (end of data), encoded using the literal/length Huffman code"
	if lit_lengths[256] == 0:
		raise InflateError("rfc1951-3.2.7#end-of-block-code", "end-of-block (256) has no code", out)
	lit, litbits, left = _code(lit_lengths)
	# Not in the RFC, which describes no incomplete literal/length code; zlib (inftrees.c: an incomplete code
	# "is only allowed for single length 1 code") and puff.c accept a single literal/length code of one bit,
	# which can only be the end-of-block code of an empty block.
	if left and sum(1 for n in lit_lengths if n) == 1 and litbits == 1:
		report.fault("rfc1951-3.2.2#one-literal-code", "a literal/length code of a single one-bit code (an empty block)")
	elif left:
		raise InflateError("rfc1951-3.2.2#incomplete", "the literal/length code is incomplete", out)
	dist, distbits, left = _code(dist_lengths)
	used = sum(1 for n in dist_lengths if n)
	# "One distance code of zero bits means that there are no distance codes used at all (the data is all literals)."
	if used == 0:
		report.reach("rfc1951-3.2.7#no-distance-codes")
	# "If only one distance code is used, it is encoded using one bit, not zero bits; in this case there is a
	# single code length of one, with one unused code."
	elif used == 1 and distbits == 1:
		report.reach("rfc1951-3.2.7#one-distance-code")
	elif left:
		raise InflateError("rfc1951-3.2.2#incomplete", "the distance code is incomplete", out)
	return lit, litbits, dist, distbits


def _huffman(b: _Bits, out: bytearray, lit: list[int], litbits: int, dist: list[int], distbits: int,
		start: int, window: int, report: StreamDiagnostics, observer=None) -> None:
	"""3.2.3 / 3.2.5: decode literal/length and distance codes until end-of-block.

	The common case is inlined: one table lookup per code, refilling the bit buffer 6 bytes at a time
	(48 bits cover a length code, its extra bits, a distance code and its extra bits).
	"""
	data, n = b.data, len(b.data)
	pos, buf, cnt = b.pos, b.buf, b.cnt
	litmask, distmask = (1 << litbits) - 1, (1 << distbits) - 1
	while True:
		if cnt < 48 and pos < n:
			take = min(6, n - pos)
			buf |= int.from_bytes(data[pos:pos + take], "little") << cnt
			pos += take
			cnt += 8 * take
		e = lit[buf & litmask]
		if e < 0 or e & 15 > cnt:
			b.pos, b.buf, b.cnt = pos, buf, cnt
			_decode(b, lit, litbits, out, "literal/length")  # raises with the precise reason
			raise AssertionError("_decode accepted a code the inline lookup refused")
		buf >>= e & 15
		cnt -= e & 15
		sym = e >> 4
		if sym < 256:
			out.append(sym)
			continue
		if sym == 256:
			b.pos, b.buf, b.cnt = pos, buf, cnt
			return
		# 3.2.6 "Literal/length values 286-287 will never actually occur in the compressed data"
		if sym > 285:
			raise InflateError("rfc1951-3.2.6#literal-286-287", f"literal/length code {sym} occurs in the data", out)
		i = sym - 257
		extra = LENGTH_EXTRA[i]
		if extra > cnt:
			raise Truncated("rfc1951-3.2.3#end-of-input", "input ends inside length extra bits", out)
		length = LENGTH_BASE[i] + (buf & ((1 << extra) - 1))
		buf >>= extra
		cnt -= extra
		# 3.2.5 table: "284 5 227-257": the extra bits 11111 would give 258, which the table does not list
		# (258 is code 285).
		if sym == 284 and length == 258:
			raise InflateError("rfc1951-3.2.5#length-range", "length code 284 with extra bits 31 (258) is outside 227-257", out)
		e = dist[buf & distmask]
		if e < 0 or e & 15 > cnt:
			b.pos, b.buf, b.cnt = pos, buf, cnt
			_decode(b, dist, distbits, out, "distance")  # raises with the precise reason
			raise AssertionError("_decode accepted a code the inline lookup refused")
		buf >>= e & 15
		cnt -= e & 15
		dsym = e >> 4
		# 3.2.5 defines distance codes 0-29; 3.2.6 "distance codes 30-31 will never actually occur in the compressed data."
		if dsym > 29:
			raise InflateError("rfc1951-3.2.5#distance-30-31", f"distance code {dsym} occurs in the data", out)
		extra = DIST_EXTRA[dsym]
		if extra > cnt:
			raise Truncated("rfc1951-3.2.3#end-of-input", "input ends inside distance extra bits", out)
		d = DIST_BASE[dsym] + (buf & ((1 << extra) - 1))
		buf >>= extra
		cnt -= extra
		# 3.2.3 "a distance cannot refer past the beginning of the output stream."
		if d > len(out):
			raise InflateError("rfc1951-3.2.3#distance-too-far", f"distance {d} with only {len(out)} bytes output", out)
		if observer is not None:
			observer.reference(len(out), d)
		# 3.2.3 "the backward distance may cross one or more block boundaries"
		if d > len(out) - start:
			report.reach("rfc1951-3.2.3#cross-block")
		# RFC 1950 2.2 "CINFO is the base-2 logarithm of the LZ77 window size, minus eight": nothing tells a
		# decompressor to enforce it, so a distance past the declared window (within 32768) is decoded.
		if d > window:
			report.fault("rfc1950-2.2#window-exceeded", "a distance reaches further back than the window CINFO declares")
		# 3.2.3 "the referenced string may overlap the current position"
		if d >= length:
			out += out[-d:len(out) - d + length]
		else:
			report.reach("rfc1951-3.2.3#overlap")
			out += (out[-d:] * (length // d + 1))[:length]
