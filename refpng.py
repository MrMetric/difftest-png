"""Reference PNG decoder for the conformance suite: PNG 2nd edition (W3C REC-PNG-20031110 =
ISO/IEC 15948:2003) and W3C PNG 3rd edition (2025).

	python refpng.py [--edition 2|3] in.png out.pam
	python refpng.py --version

decode(data, edition=3) returns an Image or raises Reject. Image.samples is a flat, row-major
array of RGBA samples: array('B') when maxval is 255 (bit depth 8 or less), array('H') when it
is 65535. Palette expanded, tRNS applied (alpha 0 at matching pixels, else maxval), sub-byte
greyscale scaled exactly to 8 bits; nothing else (no gamma, background or sBIT).

Each check names a clause of spec/clauses.tsv and quotes the sentence it enforces:
	d.require(ok, id, msg)   the datastream is rejected (Reject)
	d.expect(ok, id, msg)    the fault is recovered from, with a warning (Image.warnings)
	d.reach(id)              a consequence=none rule the datastream exercised
All three write one Diagnostics record. Reached clauses and warnings are read-only projections.
Known chunk layouts are declared once in CHUNKS; the generic parser validates their typed fields.
Handlers receive Values, never raw chunk bodies.
Section numbers are the 3rd edition's. Checks whose required behaviour differs between the
editions branch on d.edition and quote both texts; chunks the 3rd edition adds (cICP, mDCV,
cLLI, acTL, fcTL, fdAT) are unknown ancillary chunks under edition 2. Registered extensions,
including eXIf (2017), are recognized in both editions; iDOT is recognized only in edition 3.

Policies where the specification leaves the decoder a choice (13.1: "The PNG decoder has to
determine whether a syntax error is fatal (unrecoverable) or not"):
- CRC mismatch: fatal on a critical chunk, except that PLTE in a truecolour image and IEND are
  treated like ancillary chunks (13.1 says both can be ignored when invalid); an ancillary chunk
  with a bad CRC is dropped with a warning. A dropped chunk's content is not used, but it still
  occupies its place in the chunk sequence (it ends a run of IDAT chunks; it can precede IHDR).
- Ancillary chunks: a malformed, misplaced or duplicate chunk is ignored with a warning (13.2
  recommends ignoring unexpected values in ancillary chunks). The second occurrence of a chunk
  that may occur once is a duplicate even if the first was ignored as malformed or misplaced.
- Critical chunks out of place, unknown critical chunks, and invalid image data are fatal.
- Image data: too little is fatal; too much, bytes after the zlib stream, and a zlib stream that
  stops or becomes invalid after the image is complete are warnings. A wrong
  Adler-32 is fatal (RFC 1950 2.3).
- A truncated chunk is fatal; a datastream that ends cleanly after IDAT without IEND, and bytes
  after IEND, are warnings.
- Palette index beyond PLTE, the one place the editions contradict each other: edition 3 renders
  opaque black with a warning (13.1); edition 2 calls it an error, which its 13.3 says shall be
  treated as one and recommends be fatal in a critical chunk, so the reference rejects.
- tRNS / bKGD sample bits above the bit depth are masked off in both editions: the 2nd edition's
  "the least significant bits are used" is the instruction the 3rd edition spells out as "decoders
  must mask the other bits to 0".
- APNG chunks are checked for structure only; every fault is a warning and the static image is
  decoded (13.1: "A decoder which detects an error before the animation has started should
  display the static image").
"""
import struct
import sys
import zlib
from array import array
from itertools import accumulate
from dataclasses import dataclass

from pngdiagnostics import Diagnostics, StreamDiagnostics
from pngfields import (Parser, Values, Ignored, Layout, Struct, UInt, BYTE, UINT16, UINT31, enum,
	Repeat, Choice, Text, Compressed, Content, Fragment, RemainderLength, Sample, SInt32, AppleUInt32, Rule, TextList, positive_float)

import inflate

VERSION = "refpng 1.0 (PNG 2nd and 3rd edition reference decoder, own inflate)"
SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
CT_NAME = {0: "g", 2: "rgb", 3: "p", 4: "ga", 6: "rgba"}
# 8.1 Adam7: (x start, y start, x step, y step) of passes 1-7.
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
# 5.6 Table 7 (2nd ed. Table 5.3 is the same without cICP, mDCV, cLLI, eXIf, acTL, fcTL and fdAT).
BEFORE_PLTE = {b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"cICP", b"mDCV", b"cLLI"}
AFTER_PLTE = {b"bKGD", b"hIST", b"tRNS"}
BEFORE_IDAT = {b"pHYs", b"sPLT", b"eXIf", b"acTL"} | BEFORE_PLTE | AFTER_PLTE
SINGLE = {b"IHDR", b"PLTE", b"IEND", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"cICP", b"mDCV", b"cLLI",
	b"bKGD", b"hIST", b"tRNS", b"pHYs", b"tIME", b"eXIf", b"acTL"}
# 11.3.2.5 Table 17: the cHRM values written with sRGB, in chunk order (white x, y, red x, y, green x, y, blue x, y).
SRGB_CHRM = (31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000)
# Registered names whose content formats are outside this reference's scope.
REGISTERED_OPAQUE = {b"dSIG": "ext-5.1#dsig", b"fRAc": "ext-5.2#frac"}
EDITION3_CHUNKS = {b"cICP", b"mDCV", b"cLLI", b"acTL", b"fcTL", b"fdAT", b"iDOT"}

# The same ordering tables carry extension clauses and decoder consequences.
# Extension 1.6.1 section 3: "Multiple OK? No", "Before IDAT" for oFFs, pCAL, sCAL and sTER.
SINGLE = dict.fromkeys(SINGLE, "5.6#single-ancillary")
BEFORE_IDAT = dict.fromkeys(BEFORE_IDAT, "5.6#before-idat")
for _name in (b"oFFs", b"pCAL", b"sCAL", b"sTER"):
	SINGLE[_name] = "ext-3#single"
	BEFORE_IDAT[_name] = "ext-3#before-idat"
# Owner's iDOT rule: "one iDOT only; before the first IDAT".
SINGLE[b"iDOT"] = "idot#single"
BEFORE_IDAT[b"iDOT"] = "idot#before-idat"


class Reject(Exception):
	"""A fatal diagnostic already committed to the decode's record."""
	def __init__(self, id: str, msg: str, diagnostics: Diagnostics):
		super().__init__(msg)
		assert any(e.id == id and e.level == "ERR" for e in diagnostics.events), "unrecorded rejection"
		self.id, self.diagnostics = id, diagnostics

	@property
	def reached(self):
		return self.diagnostics.reached

	@property
	def warnings(self):
		return self.diagnostics.warnings


class Image:
	def __init__(self, width: int, height: int, maxval: int, samples: array, diagnostics: Diagnostics):
		self.width, self.height, self.maxval, self.samples = width, height, maxval, samples
		self.diagnostics = diagnostics

	@property
	def reached(self):
		return self.diagnostics.reached

	@property
	def warnings(self):
		return self.diagnostics.warnings


class _State:
	"""Semantic image state; every observation belongs to its Diagnostics record."""

	def __init__(self, edition: int, diagnostics: Diagnostics):
		self.edition = edition
		self.diagnostics = diagnostics
		self.width = self.height = self.depth = self.ctype = self.interlace = 0
		self.plte: list[tuple[int, ...]] | None = None
		self.trns: tuple[int, ...] | None = None  # alpha table (type 3) or key sample values (types 0, 2)
		self.hist: tuple[int, ...] | None = None
		self.seen: set[bytes] = set()  # known chunk types met so far with a good CRC, placed correctly or not
		self.chunk_count = 0
		self.idat_run = 0  # 0 before IDAT, 1 within the IDAT run, 2 after it
		self.idat = bytearray()
		self.idat_sizes: list[int] = []
		self.chunk_offset = 0
		self.idat_offsets: dict[int, int] = {}  # file offset -> offset in the concatenated zlib stream
		self.idot = None
		self.segment_rows: set[int] = set()
		self.iend = False
		self.splt_names: set[str] = set()
		self.gama: int | None = None  # well-formed values, for the sRGB compatibility check
		self.chrm: tuple[int, ...] | None = None
		self.srgb = False
		self.apng: list[tuple] = []  # ("acTL", fields) / ("fcTL", fields, after_idat) / ("fdAT", seq, after_idat)

	def reach(self, id: str) -> None:
		self.diagnostics.record(id)

	def require(self, ok: bool, id: str, msg: str) -> None:
		if not ok:
			self.diagnostics.record(id, "ERR", msg)
			raise Reject(id, msg, self.diagnostics)

	def expect(self, ok: bool, id: str, msg: str) -> bool:
		if not ok:
			self.diagnostics.record(id, "WARN", msg)
		return ok


def decode(data: bytes, edition: int = 3, *, diagnostics: Diagnostics | None = None) -> Image:
	"""Decode a PNG. The caller may own the record, including across unexpected exceptions."""
	assert edition in (2, 3), edition
	record = diagnostics if diagnostics is not None else Diagnostics()
	assert not record.events, "each decode needs a fresh diagnostic record"
	d = _State(edition, record)
	try:
		_signature(d, data)
		for ctype, body, crc_ok, placed in _chunks(d, data):
			_chunk(d, ctype, body, crc_ok, placed)
		_datastream_end(d)
		raw, incomplete = _decompress(d)
		rows = _scanlines(d, raw, incomplete)
		_index_range(d, rows)
		_hist_usage(d, rows)
		assert all(e.level != "ERR" for e in record.events), "fatal diagnostic cannot return an image"
		return _output(d, rows)
	except Exception as e:
		# Keep the accumulated record accessible even for programming errors and MemoryError.
		e.diagnostics = record
		raise


# ---------------------------------------------------------------- 5 Datastream structure

def _signature(d: _State, data: bytes) -> None:
	# 5.2 "The first eight bytes of a PNG datastream always contain the following hexadecimal values:
	# 89 50 4E 47 0D 0A 1A 0A"; 13.1 "Decoders should verify that all eight bytes of the PNG signature are correct."
	d.require(data[:8] == SIGNATURE, "5.2#signature", f"signature {data[:8].hex()}")


def _chunks(d: _State, data: bytes):
	"""5.3 Chunk layout: Length, Chunk Type, Chunk Data, CRC. Yields (type, data, CRC correct, structurally usable)."""
	pos = 8
	while pos < len(data):
		if d.iend:
			# 15.3.1 "No chunks or other content follow the IEND chunk." 13.3 "there could be data after the
			# IEND chunk": the reference ignores it.
			d.expect(False, "11.2#iend-last", f"{len(data) - pos} bytes after IEND ignored")
			return
		# 13.1 "an unexpected end-of-stream indicates a corrupted datastream, and may be regarded as a fatal error."
		d.require(pos + 8 <= len(data), "13.1#end-of-stream", f"datastream ends inside a chunk header at byte {pos}")
		length = int.from_bytes(data[pos:pos + 4], "big")
		ctype = data[pos + 4:pos + 8]
		# 5.3 "Although encoders and decoders should treat the length as unsigned, its value shall not exceed 2^31-1 bytes."
		d.require(length <= 0x7FFFFFFF, "5.3#length-limit", f"chunk length {length} exceeds 2^31-1")
		d.chunk_offset = pos
		placed = _structure(d, ctype, length)
		# 5.3 "chunk types are restricted to consist of bytes with values in the ranges 41-5A and 61-7A";
		# 13.2 "The chunk type can be checked for plausibility": the reference treats a failure as corruption.
		d.require(all(65 <= c <= 90 or 97 <= c <= 122 for c in ctype), "5.3#type-letters", f"chunk type {ctype!r} is not four letters")
		d.require(pos + 12 + length <= len(data), "13.1#end-of-stream", f"{ctype.decode()} chunk of {length} bytes runs past the end of the datastream")
		body = data[pos + 8:pos + 8 + length]
		stored = int.from_bytes(data[pos + 8 + length:pos + 12 + length], "big")
		pos += 12 + length
		# 5.3 "Zero is a valid length."
		if length == 0 and ctype != b"IEND":
			d.reach("5.3#zero-length")
		yield ctype, body, _crc(d, ctype, body, stored), placed
	if not d.iend:
		# 11.2 "A valid PNG datastream shall ... end with an IEND chunk."
		d.require(d.idat_run > 0, "11.2#idat-present", "datastream ends before any IDAT chunk")
		d.expect(False, "11.2#iend-present", "datastream ends without an IEND chunk")


def _crc(d: _State, ctype: bytes, body: bytes, stored: int) -> bool:
	"""5.5 CRC algorithm; 13.1 error handling. Returns whether the chunk's content is used."""
	# 5.3 CRC "calculated on the preceding bytes in the chunk, including the chunk type field and chunk data
	# fields, but not including the length field"
	if zlib.crc32(ctype + body) == stored:
		return True
	critical = not ctype[0] & 0x20
	# 13.1 "A PNG signature mismatch, a CRC mismatch, or an unexpected end-of-stream indicates a corrupted
	# datastream, and may be regarded as a fatal error." 13.1 "most decoders can ignore an invalid IEND chunk;
	# ... it can ignore an invalid PLTE chunk in a truecolor image".
	if critical:
		ignorable = ctype == b"IEND" or (ctype == b"PLTE" and d.ctype in (2, 6))
		d.require(ignorable, "13.1#crc-critical", f"CRC mismatch on critical chunk {ctype.decode()}")
		d.expect(False, "13.1#crc-critical", f"CRC mismatch on {ctype.decode()}" + ("; chunk dropped" if ctype == b"PLTE" else ""))
		return ctype == b"IEND"
	d.expect(False, "13.1#crc-ancillary", f"CRC mismatch on ancillary chunk {ctype.decode()}; chunk dropped")
	return False


def _structure(d: _State, ctype: bytes, length: int) -> bool:
	"""Type, position and declared length are checked before type plausibility, payload and CRC."""
	d.chunk_count += 1
	name = ctype.decode() if ctype.isalpha() else repr(ctype)
	# 11.2.1 "The IHDR chunk shall be the first chunk in the PNG datastream."
	d.require(d.chunk_count > 1 or ctype == b"IHDR", "11.2#ihdr-first", f"first chunk is {name}, not IHDR")
	if d.idat_run == 1 and ctype != b"IDAT":
		d.idat_run = 2
	chunk = CHUNKS.get(ctype)
	_naming(d, ctype, ctype in REGISTERED_OPAQUE or chunk is not None and (d.edition == 3 or ctype not in EDITION3_CHUNKS))
	if chunk is None or (ctype in EDITION3_CHUNKS and d.edition < 3):
		if ctype in REGISTERED_OPAQUE:
			d.reach(REGISTERED_OPAQUE[ctype])
		else:
			_unknown(d, ctype)
		return False
	if not _ordering(d, ctype) or not _content_allowed(d, ctype):
		return False
	if ctype == b"IDAT":
		# 11.2.1 "Each pixel is a palette index; a PLTE chunk shall appear." Table 7: "Before first IDAT".
		d.require(d.ctype != 3 or d.plte is not None, "11.2.2#required", "indexed-colour image data without a preceding PLTE")
		# 11.2.3 "they shall appear consecutively with no other intervening chunks."
		d.require(d.idat_run != 2, "11.2.3#consecutive", "IDAT chunks are not consecutive")
	if ctype == b"IEND":
		# 11.2 "one or more IDAT chunks, and shall end with an IEND chunk."
		d.require(d.idat_run > 0, "11.2#idat-present", "IEND before any IDAT chunk")
	fatal = ctype == b"IHDR" or (ctype == b"PLTE" and d.ctype == 3)
	layout = chunk.layout(d) if callable(chunk.layout) else chunk.layout
	return Parser(d, ctype, b"", layout, fatal).check_length(length)


def _chunk(d: _State, ctype: bytes, body: bytes, crc_ok: bool, placed: bool) -> None:
	"""Dispatch structurally usable chunks after checking their CRC and typed content."""
	if not crc_ok:
		return
	chunk = CHUNKS.get(ctype)
	if chunk is None or (ctype in EDITION3_CHUNKS and d.edition < 3):
		return
	d.seen.add(ctype)
	if not placed:
		return
	if d.idat_run == 2 and ctype[0] & 0x20:
		# 14.3.2 "Decoders shall not assume more about the positioning of any ancillary chunk than is specified by
		# the chunk ordering rules."
		d.reach("14.3.2#ancillary-position")
	fatal = ctype == b"IHDR" or (ctype == b"PLTE" and d.ctype == 3)
	layout = chunk.layout(d) if callable(chunk.layout) else chunk.layout
	try:
		fields = Parser(d, ctype, body, layout, fatal).parse()
	except Ignored:
		return
	chunk.handler(d, fields)


def _content_allowed(d: _State, ctype: bytes) -> bool:
	"""Context rules checked before interpreting a known chunk's declared layout."""
	if ctype == b"PLTE":
		# 11.2.2 "it shall not appear for color types 0 and 4."
		if not d.expect(d.ctype not in (0, 4), "11.2.2#forbidden", f"PLTE in colour type {d.ctype}; ignored"):
			return False
		# 5.6 Table 7: "Before first IDAT"; the required indexed PLTE is checked at IDAT.
		return d.expect(not d.idat_run, "11.2.2#invalid-truecolour", "suggested PLTE after IDAT; ignored")
	if ctype == b"tRNS":
		# 11.3.1.1 "A tRNS chunk shall not appear for color types 4 and 6".
		return d.expect(d.ctype not in (4, 6), "11.3.1.1#forbidden", f"tRNS in colour type {d.ctype}; ignored")
	if ctype == b"hIST":
		# 11.3.4.2 "There shall be exactly one entry for each entry in the PLTE chunk."
		return d.expect(bool(d.plte), "11.3.4.2#entries", "hIST without a palette; ignored")
	if ctype == b"fdAT" and not d.idat_run:
		# 5.6 Table 7: fdAT "After IDAT".
		_apng_fault(d, "11.3.6.3#after-idat", "fdAT before IDAT ignored")
		return False
	return True


def _naming(d: _State, ctype: bytes, defined: bool) -> None:
	"""Naming conformance is independent of content dispatch and CRC policy."""
	name = repr(ctype)
	# 5.4 Table 6 "If the reserved bit is 1, the datastream does not conform to this version of PNG."
	d.expect(not ctype[2] & 0x20, "5.4#reserved-bit", f"reserved bit set in {name}")
	# 5.7.2 "A proposed public chunk type SHALL not be used in publicly available software or datastreams until defined as such".
	d.expect(defined or bool(ctype[1] & 0x20), "5.7.2#undefined-public", f"undefined public chunk {name}")


def _unknown(d: _State, ctype: bytes) -> None:
	"""5.4 / 13.5: a chunk type this edition does not define."""
	fold = bytes(c & 0xDF for c in ctype)
	known = [k for k in CHUNKS if k != ctype and (d.edition >= 3 or k not in EDITION3_CHUNKS)]
	# 13.5 "Decoders shall recognize chunk types by a simple four-byte literal comparison; it is incorrect to
	# perform case conversion on chunk types."
	if any(fold == bytes(c & 0xDF for c in k) for k in known):
		d.reach("13.5#literal-comparison")
	# 13.5 "Decoders should not flag an error if the reserved bit is set to 1 ... It is sufficient to treat a
	# chunk with this bit set in the same way as any other unknown chunk type."
	if ctype[2] & 0x20:
		d.reach("13.5#reserved-bit")
	# 13.1 "Encountering an unknown critical chunk is a fatal condition for any decoder trying to extract the
	# image from the datastream." 13.5 "A decoder trying to extract the image, upon encountering an unknown chunk
	# in which the ancillary bit is 0, indicating a critical chunk, shall indicate to the user that the image
	# contains information it cannot safely interpret."
	name = ctype.decode() if ctype.isalpha() else repr(ctype)
	d.require(bool(ctype[0] & 0x20), "5.4#unknown-critical", f"unknown critical chunk {name}")
	# 13.1 "Encountering an unknown ancillary chunk is never an error. The chunk can simply be ignored."
	d.reach("5.4#unknown-ancillary")
	if d.idat_run == 2:
		d.reach("14.3.2#ancillary-position")


def _ordering(d: _State, ctype: bytes) -> bool:
	"""5.6 Chunk ordering (Table 7). Returns whether the chunk is processed."""
	name = ctype.decode()
	if ctype in SINGLE and ctype in d.seen:
		# 11.2 "Only one IHDR chunk is allowed"; 11.2.2 "There shall not be more than one PLTE chunk."
		d.require(ctype != b"IHDR", "11.2#one-ihdr", "second IHDR chunk")
		if ctype == b"PLTE":
			d.require(d.ctype != 3, "11.2.2#single", "second PLTE chunk in an indexed-colour image")
			d.expect(False, "11.2.2#single", "second (suggested) PLTE chunk ignored")
			return False
		# Table 7 "Multiple allowed: No"; 13.1 names "the presence of multiple gAMA chunks" as a syntax error.
		d.expect(False, SINGLE[ctype], f"second {name} chunk ignored")
		return False
	if ctype in BEFORE_PLTE and (d.plte is not None or d.idat_run):
		# Table 7 "Before PLTE and IDAT"
		d.expect(False, "5.6#before-plte", f"{name} after {'IDAT' if d.idat_run else 'PLTE'} ignored")
		return False
	if ctype in AFTER_PLTE and (d.idat_run or (d.ctype == 3 and d.plte is None)):
		# Table 7 "After PLTE; before IDAT"
		d.expect(False, "5.6#after-plte", f"{name} {'after IDAT' if d.idat_run else 'before PLTE'} ignored")
		return False
	# Extension 1.5.0 3.7: "anywhere between the IHDR and IEND chunks except between IDAT chunks".
	if ctype in BEFORE_IDAT and d.idat_run and not (ctype == b"eXIf" and d.edition == 2):
		# Table 7 "Before IDAT"
		if ctype == b"acTL":
			# 11.3.6.1 "The acTL chunk must appear before the first IDAT chunk within a valid PNG stream."
			_apng_fault(d, "11.3.6.1#before-idat", "acTL after IDAT ignored")
		else:
			d.expect(False, BEFORE_IDAT[ctype], f"{name} after IDAT ignored")
		return False
	if ctype == b"PLTE" and d.ctype in (2, 6) and d.seen & AFTER_PLTE:
		# Table 7 "After PLTE": a chunk already processed before a suggested PLTE is kept.
		d.expect(False, "5.6#after-plte", f"{b', '.join(sorted(d.seen & AFTER_PLTE)).decode()} before PLTE")
	if ctype in (b"iCCP", b"sRGB") and (b"sRGB" if ctype == b"iCCP" else b"iCCP") in d.seen:
		# Table 7 "If the iCCP chunk is present, the sRGB chunk should not be present." (and the reverse)
		d.expect(False, "5.6#iccp-srgb", "both iCCP and sRGB present")
	return True


# ---------------------------------------------------------------- 7 Scanlines, 8 Interlacing, 9 Filtering

def _reduced_images(d: _State) -> list[tuple[int, int, int, int, int, int]]:
	"""8.1 Interlace methods: (x0, y0, dx, dy, width, height) of each reduced image, empty ones included."""
	if not d.interlace:
		return [(0, 0, 1, 1, d.width, d.height)]
	out = []
	for x0, y0, dx, dy in ADAM7:
		out.append((x0, y0, dx, dy, max(0, (d.width - x0 + dx - 1) // dx), max(0, (d.height - y0 + dy - 1) // dy)))
	return out


def _scanlines(d: _State, raw: bytes, incomplete: str | None = None) -> list[list[int]]:
	"""7.2 Scanlines, 8.1 pass extraction and 9 filtering, inverted: image rows of samples."""
	nch = CHANNELS[d.ctype]
	# 9.2 "bpp is defined as the number of bytes per complete pixel, rounding up to one"
	bpp = max(1, nch * d.depth // 8)
	reduced = []
	pos = 0
	for x0, y0, dx, dy, w, h in _reduced_images(d):
		# 9.1 "Filter type bytes are associated only with non-empty scanlines. No filter type bytes are present in
		# an empty pass."
		if w == 0 or h == 0:
			d.reach("9.1#empty-pass" if h else "9.1#pass-without-rows")
			continue
		# 7.2 "Scanlines always begin on byte boundaries."
		rowbytes = (w * nch * d.depth + 7) // 8
		pad = rowbytes * 8 - w * nch * d.depth
		# 9.2 "the entire prior scanline ... shall be treated as being zeroes for the first scanline of a reduced image"
		prev = bytes(rowbytes)
		rows = []
		for j in range(h):
			# 13.1 "unexpected end-of-stream ... may be regarded as a fatal error"
			d.require(pos + 1 + rowbytes <= len(raw), "13.1#data-short",
				f"image data ends {pos + 1 + rowbytes - len(raw)} bytes short (reduced image {len(reduced) + 1}, scanline {j})"
				+ (f"; the zlib stream ended early: {incomplete}" if incomplete else ""))
			ftype = raw[pos]
			if j == 0 and ftype in (2, 3, 4):
				d.reach("9.2#first-scanline")
			# Owner's iDOT rule: "its first row isn't filtered against the previous segment's last row".
			if not d.interlace and j in d.segment_rows:
				d.expect(ftype not in (2, 3, 4), "idot#filter", f"iDOT segment row {j} needs the previous row")
			cur = _unfilter(d, ftype, raw[pos + 1:pos + 1 + rowbytes], prev, bpp)
			pos += 1 + rowbytes
			# 7.2 the unused low-order bits of the last byte: "The contents of these unused bits are not specified."
			if pad and cur[-1] & ((1 << pad) - 1):
				d.reach("7.2#padding-unspecified")
			rows.append(_unpack(cur, w * nch, d.depth))
			prev = cur
		reduced.append((x0, y0, dx, dy, w, rows))
	# 13.1 / 11.2.3: decompressed data past the last scanline is not part of the image.
	d.expect(pos == len(raw), "13.1#data-long", f"{len(raw) - pos} bytes of decompressed data after the image ignored")
	if not d.interlace:
		return reduced[0][5]
	# 8.1: place each reduced image's pixels at (x0 + i*dx, y0 + j*dy).
	image = [[0] * (d.width * nch) for _ in range(d.height)]
	for x0, y0, dx, dy, w, rows in reduced:
		for j, samples in enumerate(rows):
			row = image[y0 + j * dy]
			for c in range(nch):
				row[x0 * nch + c::dx * nch] = samples[c::nch]
	return image


def _unfilter(d: _State, ftype: int, line: bytes, prev: bytes, bpp: int) -> bytes:
	"""9.2-9.4: Recon(x) for one scanline, arithmetic modulo 256."""
	# 9.2 "Filter method 0 specifies exactly this set of five filter types and this shall not be extended."
	# 13.1 names "an invalid value for a row filter" as a syntax error; in image data it is fatal.
	d.require(ftype <= 4, "9.2#filter-types", f"filter type {ftype} is not defined for filter method 0")
	d.reach(("9.2#filter-none", "9.2#filter-sub", "9.2#filter-up", "9.2#filter-average", "9.2#filter-paeth")[ftype])
	if ftype in (1, 3, 4):
		# 9.2 "For all filters, the bytes "to the left of" the first pixel in a scanline shall be treated as being zero."
		d.reach("9.2#left-zero")
		if bpp > 1:
			# 9.2 a is "the byte corresponding to x in the pixel immediately before the pixel containing x"
			d.reach("9.2#byte-distance")
	n = len(line)
	if ftype == 0:
		return bytes(line)
	if ftype == 2:
		# Recon(x) = Filt(x) + Recon(b)
		return bytes(map((255).__and__, map(int.__add__, line, prev)))
	cur = bytearray(line)
	if ftype == 1:
		# Recon(x) = Filt(x) + Recon(a): a running sum per byte position modulo bpp
		for c in range(bpp):
			cur[c::bpp] = bytes(map((255).__and__, accumulate(line[c::bpp])))
		return bytes(cur)
	if ftype == 3:
		# Recon(x) = Filt(x) + floor((Recon(a) + Recon(b)) / 2)
		# 9.3 "The sum Orig(a) + Orig(b) shall be performed without overflow (using at least nine-bit arithmetic)."
		carry = False
		for i in range(n):
			s = (cur[i - bpp] if i >= bpp else 0) + prev[i]
			carry |= s > 255
			cur[i] = (cur[i] + (s >> 1)) & 255
		if carry:
			d.reach("9.3#average-overflow")
		return bytes(cur)
	# Recon(x) = Filt(x) + PaethPredictor(Recon(a), Recon(b), Recon(c))
	tie = wide = False
	for i in range(n):
		b = prev[i]
		if i >= bpp:
			a, c = cur[i - bpp], prev[i - bpp]
		else:
			a = c = 0
		# 9.4 "The calculations within the PaethPredictor function shall be performed exactly, without overflow."
		p = a + b - c
		wide |= p < 0 or p > 255
		pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
		# 9.4 "The order in which the comparisons are performed is critical and shall not be altered."
		# A tie decides the result only between different values; pa == pb <= pc with a != b cannot happen
		# (it forces p == c, so pc == 0, so a == b == c).
		if pa <= pb and pa <= pc:
			pred = a
			tie |= pa == pc and a != c
		elif pb <= pc:
			pred = b
			tie |= pb == pc and b != c
		else:
			pred = c
		cur[i] = (cur[i] + pred) & 255
	if tie:
		d.reach("9.4#paeth-order")
	if wide:
		d.reach("9.4#paeth-exact")
	return bytes(cur)


def _unpack(line: bytes, count: int, depth: int) -> list[int]:
	"""7.1 / 7.2: the samples of one scanline."""
	if depth == 8:
		return list(line)
	if depth == 16:
		# 7.1 "All integers that require more than one byte shall be in network byte order"
		return list(struct.unpack(f">{count}H", line))
	# 7.2 "Pixels smaller than a byte never cross byte boundaries; they are packed into bytes with the leftmost
	# pixel in the high-order bits of a byte, the rightmost in the low-order bits."
	per, mask = 8 // depth, (1 << depth) - 1
	shifts = [8 - depth * (k + 1) for k in range(per)]
	return [(line[i // per] >> shifts[i % per]) & mask for i in range(count)]


# ---------------------------------------------------------------- 10 Compression

def _decompress(d: _State) -> tuple[bytes, str | None]:
	"""10.1 Compression method 0; 10.2 / 13.8: the IDAT data fields concatenated form one zlib datastream.

	Returns (decompressed data, None) or, when a stream fault is recovered, (what was decoded, the reason)."""
	z = bytes(d.idat)
	if len(z) >= 2:
		# 10.1 "the zlib compression method/flags code shall specify method code 8 (deflate compression) and an
		# LZ77 window size of not more than 32768 bytes."
		d.require(z[0] & 15 == 8 and z[0] >> 4 <= 7, "10.1#zlib-method", f"zlib CMF {z[0]:02x}: method {z[0] & 15}, window 2^{(z[0] >> 4) + 8}")
		# 10.1 "The additional flags shall not specify a preset dictionary."
		d.require(not z[1] & 0x20, "10.1#no-preset-dictionary", "zlib FDICT flag set")
	# 7.2 "Scanlines always begin on byte boundaries"; 9.1 "No filter type bytes are present in an empty pass."
	needed = sum(h * (1 + (w * CHANNELS[d.ctype] * d.depth + 7) // 8)
		for _, _, _, _, w, h in _reduced_images(d) if w and h)
	def failure(e):
		complete = len(e.output) >= needed
		truncated = isinstance(e, inflate.Truncated)
		# 10.1 "Deflate-compressed datastreams within PNG are stored in the 'zlib' format".
		# Faults beyond the complete image have the same acceptance policy as an incomplete stream.
		if complete and e.id != "rfc1950-2.3#adler32":
			message = f"zlib stream incomplete: {e}" if truncated else f"invalid zlib stream after the image: {e}"
			d.diagnostics.record("10.1#zlib-check", "WARN", message, cause=e.id)
		elif truncated:
			# Record truncation before scanline validation diagnoses the incomplete image.
			d.diagnostics.record("10.1#zlib-check", "WARN", f"zlib stream incomplete: {e}", cause=e.id)
		else:
			d.diagnostics.record(e.id, "ERR", f"image data: {e}")
	incomplete = None
	try:
		raw, end = inflate.zlib_decompress(z, StreamDiagnostics(d.diagnostics, "image data", failure=failure), observer=_idot_segments(d))
	except inflate.InflateError as e:
		# 10.1 "Deflate-compressed datastreams within PNG are stored in the "zlib" format"; the deflate data
		# shall follow RFC 1951, the zlib framing RFC 1950.
		if d.diagnostics.events[-1].level == "ERR":
			d.reach("10.1#deflate-valid")
			raise Reject(e.id, f"image data: {e}", d.diagnostics) from e
		raw, end, incomplete = e.output, len(z), f"[{e.id}] {e}"
	_idat_boundaries(d, end)
	# 11.2.3 "Some images have unused trailing bytes at the end of the final IDAT chunk. ... A decoder should
	# ignore these trailing bytes." (The 2nd edition does not mention them; both editions warn and ignore.)
	d.expect(end == len(z), "11.2.3#trailing-bytes", f"{len(z) - end} bytes after the end of the zlib stream ignored")
	return raw, incomplete


def _idat_boundaries(d: _State, end: int) -> None:
	"""10.2 / 13.8 "IDAT chunk boundaries have no semantic significance and can occur at any point in the
	compressed datastream." Records where this datastream puts them."""
	if len(d.idat_sizes) > 1:
		d.reach("10.2#idat-boundaries")
	if 0 in d.idat_sizes:
		d.reach("10.2#idat-empty")
	offset = 0
	for n in d.idat_sizes[:-1]:
		offset += n
		if offset == 1:
			d.reach("10.2#split-zlib-header")
		if end - 4 < offset < end:
			d.reach("10.2#split-adler32")


# ---------------------------------------------------------------- 11.2 Critical chunks

def _ihdr(d: _State, f: Values) -> None:
	"""11.2.1 IHDR Image header."""
	w, h, depth, ctype, comp, filt, interlace = f.tuple()
	# 11.2.1 "Width and height give the image dimensions in pixels. They are PNG four-byte unsigned integers.
	# Zero is an invalid value."
	d.require(w > 0 and h > 0, "11.2.1#dimensions-nonzero", f"image dimensions {w}x{h}")
	# 11.2.1 Table 12 lists the allowed combinations; "PNG decoders shall support all valid combinations".
	allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}[ctype]
	d.require(depth in allowed, "11.2.1#combination", f"colour type {ctype} does not allow bit depth {depth}")
	d.width, d.height, d.depth, d.ctype, d.interlace = w, h, depth, ctype, interlace
	d.reach(f"11.2.1#{CT_NAME[ctype]}{depth}")
	if depth == 16:
		d.reach("7.1#byte-order")
	if depth < 8:
		d.reach("7.2#packing")
	if interlace:
		# 8.1 "Method 1 is called Adam7"; 13.10 decoders are required to read interlaced images.
		d.reach("8.1#adam7")


def _plte(d: _State, f: Values) -> None:
	"""11.2.2 PLTE Palette."""
	d.plte = [entry.tuple() for entry in f.entries]
	n = 3 * len(d.plte)
	if d.ctype == 3:
		# "The number of palette entries shall not exceed the range that can be represented in the image bit depth"
		d.expect(n // 3 <= 1 << d.depth, "11.2.2#entries-vs-depth",
			f"{n // 3} palette entries for bit depth {d.depth}; entries past {1 << d.depth} are unreachable")
		# "It is permissible to have fewer entries than the bit depth would allow."
		if n // 3 < 1 << d.depth:
			d.reach("11.2.2#fewer-entries")
	else:
		# "For color types 2 and 6 (truecolor and truecolor with alpha), the PLTE chunk is optional. If present, it
		# provides a suggested set of colors"
		d.reach("11.2.2#suggested")


def _idat(d: _State, f: Values) -> None:
	"""11.2.3 IDAT Image data."""
	d.idat_run = 1
	d.idat_offsets[d.chunk_offset] = len(d.idat)
	d.idat += f.stream
	d.idat_sizes.append(len(f.stream))


def _iend(d: _State, f: Values) -> None:
	"""11.2.4 IEND Image trailer."""
	# "The chunk's data field is empty." 13.1 "most decoders can ignore an invalid IEND chunk"
	d.expect(not f.extra, "11.2#iend-last", f"IEND has {f.extra} data bytes; ignored")
	d.iend = True


def _datastream_end(d: _State) -> None:
	"""Checks that need the whole chunk sequence."""
	if b"mDCV" in d.seen:
		# 11.3.2.7 "a cICP chunk must accompany the use of mDCV in order to establish the basic characteristics of
		# the image content."
		d.expect(b"cICP" in d.seen, "11.3.2.7#with-cicp", "mDCV without cICP")
	if d.srgb:
		# 11.3.2.5 (2nd ed. 11.3.3.5) a gAMA or cHRM written with sRGB for compatibility: "Only the following
		# values shall be used." (Table 17). Decoders that use sRGB ignore both, so only the warning follows.
		d.expect(d.gama in (None, 45455), "11.3.2.5#compat-values", f"gAMA {d.gama} with sRGB (Table 17: 45455)")
		d.expect(d.chrm in (None, SRGB_CHRM), "11.3.2.5#compat-values", f"cHRM {d.chrm} with sRGB (Table 17: {SRGB_CHRM})")
	_apng_structure(d)


def _index_range(d: _State, rows: list[list[int]]) -> None:
	"""11.2.2 "any out-of-range pixel value found in the image data is an error." (both editions)"""
	if d.ctype != 3:
		return
	limit = len(d.plte)
	bad = sum(1 for row in rows for v in row if v >= limit) if limit < 1 << d.depth else 0
	if not bad:
		return
	if d.edition == 2:
		# 2nd ed. 11.2.3 "any out-of-range pixel value found in the image data is an error"; 2nd ed. 13.3:
		# "Unexpected values in fields of known chunks ... shall be checked for and treated as errors. However, it is
		# recommended that unexpected field values be treated as fatal errors only in critical chunks." Image data
		# is a critical chunk.
		d.require(False, "11.2.2#index-in-range", f"{bad} pixels index past the {limit}-entry palette")
	# 13.1 "if out-of-range indexes are encountered ... Displaying the pixel as opaque black is one common error
	# recovery tactic, and is now required by this specification."
	d.expect(False, "13.1#index-opaque-black", f"{bad} pixels index past the {limit}-entry palette; rendered opaque black")


# ---------------------------------------------------------------- 11.3 Ancillary chunks

def _trns(d: _State, f: Values) -> None:
	"""11.3.1.1 tRNS Transparency (2nd ed. 11.3.2.1)."""
	n = len(f.samples)
	if d.ctype == 3:
		# "The tRNS chunk shall not contain more alpha values than there are palette entries, but a tRNS chunk may
		# contain fewer values than there are palette entries."
		if not d.expect(n <= len(d.plte), "11.3.1.1#length", f"tRNS has {n} alpha values for {len(d.plte)} palette entries; ignored"):
			return
		if n < len(d.plte):
			d.reach("11.3.1.1#partial-palette")
		d.trns = f.samples
		return
	key = f.samples
	if d.depth == 16:
		# 15.3.3 "When processing 16-bit greyscale or truecolor data in the tRNS chunk, both bytes of the sample
		# values are evaluated to determine whether a pixel is transparent."
		d.reach("15.3.3#trns-both-bytes")
	elif d.depth < 8:
		# 13.12 "When comparing pixel values to tRNS chunk values to detect transparent pixels, the comparison
		# shall be done exactly ... before any rescaling"
		d.reach("13.12#exact-compare")
	d.trns = key


def _chrm(d: _State, f: Values) -> None:
	"""11.3.2.1 cHRM: eight PNG four-byte unsigned integers."""
	d.chrm = f.points
	d.reach("11.3.2.1#chrm")

def _gama(d: _State, f: Values) -> None:
	# 13.13 "A gAMA chunk containing zero is meaningless ... Decoders should ignore it".
	if d.expect(f.gamma != 0, "11.3.2.2#zero", "gAMA value 0 ignored"):
		d.gama = f.gamma
		d.reach("11.3.2.2#gama")

def _iccp(d: _State, f: Values) -> None:
	# 11.3.2.3 "shall be an RGB color space for color images ... or a greyscale color space for greyscale images".
	want = "GRAY" if d.ctype in (0, 4) else "RGB "
	if d.expect(f.profile.colour_space == want, "11.3.2.3#colour-space",
			f"iCCP colour space {f.profile.colour_space!r}, expected {want!r}; ignored"):
		d.reach("11.3.2.3#iccp")


def _sbit(d: _State, f: Values) -> None:
	# 11.3.2.4 "Each depth ... shall be greater than zero and less than or equal to the sample depth".
	limit = 8 if d.ctype == 3 else d.depth
	if d.expect(all(0 < v <= limit for v in f.depths), "11.3.2.4#range", f"sBIT outside 1..{limit}; ignored"):
		d.reach("11.3.2.4#sbit")

def _srgb(d: _State, f: Values) -> None:
	d.srgb = True
	d.reach("11.3.2.5#srgb")

def _cicp(d: _State, f: Values) -> None:
	d.reach("11.3.2.6#cicp")

def _mdcv(d: _State, f: Values) -> None:
	"""11.3.2.7 mDCV Mastering Display Color Volume (3rd edition): 24 bytes; see _datastream_end for cICP."""
	d.reach("11.3.2.7#mdcv")


def _clli(d: _State, f: Values) -> None:
	"""11.3.2.8 cLLI Content Light Level Information (3rd edition): 8 bytes."""
	d.reach("11.3.2.8#clli")


def _text(d: _State, f: Values) -> None:
	d.reach("11.3.3.2#text")

def _ztxt(d: _State, f: Values) -> None:
	d.reach("11.3.3.3#ztxt")

def _itxt(d: _State, f: Values) -> None:
	if f.flag == 0 and f.method != 0:
		# 11.3.3.4 "For uncompressed text ... decoders shall ignore it."
		d.reach("11.3.3.4#method-ignored")
	d.reach("11.3.3.4#itxt")

def _bkgd(d: _State, f: Values) -> None:
	if d.ctype == 3:
		# 11.3.4.1 "the value is the palette index of the color to be used as background".
		if not d.expect(f.samples[0] < len(d.plte), "11.3.4.1#index", f"bKGD index {f.samples[0]} past the palette; ignored"):
			return
	d.reach("11.3.4.1#bkgd")

def _hist(d: _State, f: Values) -> None:
	d.hist = f.frequencies
	d.reach("11.3.4.2#hist")

def _hist_usage(d: _State, rows: list[list[int]]) -> None:
	# 11.3.4.2 "A histogram entry shall be nonzero if there are any pixels of that color."
	if d.hist is None or d.ctype != 3:
		return
	used = set().union(*map(set, rows))
	zero = sorted(i for i in used if i < len(d.hist) and d.hist[i] == 0)
	d.expect(not zero, "11.3.4.2#nonzero", f"hIST entries {zero[:8]} are zero but those palette indices are used")


def _phys(d: _State, f: Values) -> None:
	d.reach("11.3.4.3#phys")

def _splt(d: _State, f: Values) -> None:
	# 11.3.4.4 "Multiple sPLT chunks are permitted, but each shall have a different palette name."
	if not d.expect(f.name not in d.splt_names, "11.3.4.4#unique-name", f"second sPLT named {f.name!r}; ignored"):
		return
	d.splt_names.add(f.name)
	# "Entries shall appear in decreasing order of frequency." (equal frequencies allowed)
	freq = [entry.frequency for entry in f.entries]
	if d.expect(all(a >= b for a, b in zip(freq, freq[1:])), "11.3.4.4#order", "sPLT entries are not in decreasing frequency; ignored"):
		d.reach("11.3.4.4#splt")

def _exif(d: _State, f: Values) -> None:
	d.reach("11.3.4.5#exif")

def _time(d: _State, f: Values) -> None:
	d.reach("11.3.5.1#time")

def _apng_fault(d: _State, id: str, msg: str) -> None:
	# 13.1 "A decoder which detects an error before the animation has started should display the static image."
	d.reach("13.1#apng-fallback")
	d.expect(False, id, msg + "; the static image is decoded")


def _actl(d: _State, f: Values) -> None:
	d.apng.append(("acTL", f.tuple()))

def _fctl(d: _State, f: Values) -> None:
	d.apng.append(("fcTL", f.tuple(), d.idat_run > 0))

def _fdat(d: _State, f: Values) -> None:
	d.apng.append(("fdAT", f.sequence, True))

def _apng_structure(d: _State) -> None:
	"""4.9 and 11.3.6: the animation chunks taken together."""
	if not d.apng:
		return
	actl = [e[1] for e in d.apng if e[0] == "acTL"]
	fctl = [e for e in d.apng if e[0] == "fcTL"]
	# 4.9.1 "To be recognized as an APNG, an acTL chunk must appear in the stream before any IDAT chunks."
	if not actl:
		_apng_fault(d, "11.3.6.1#missing", "fcTL or fdAT without a valid acTL before IDAT")
	else:
		frames = actl[0][0]
		# 11.3.6.1 num_frames: "This must equal the number of fcTL chunks. 0 is not a valid value."
		if frames == 0:
			_apng_fault(d, "11.3.6.1#num-frames-zero", "acTL num_frames is 0")
		elif frames != len(fctl):
			_apng_fault(d, "11.3.6.1#num-frames", f"acTL num_frames {frames} but {len(fctl)} fcTL chunks")
		d.reach("11.3.6.1#actl")
	before = [e for e in fctl if not e[2]]
	# Table 7 fcTL: "One may occur before IDAT; all others shall be after IDAT"
	if len(before) > 1:
		_apng_fault(d, "11.3.6.2#one-before-idat", f"{len(before)} fcTL chunks before IDAT")
	if actl:
		# 4.9.1 "The static image may be included as the first frame of the animation by the presence of a single
		# fcTL chunk before IDAT. Otherwise, the static image is not part of the animation."
		d.reach("4.9.1#static-image-first-frame" if before else "4.9.1#static-image-not-in-animation")
	for i, (_, (seq, w, h, x, y, dnum, dden, dispose, blend), after) in enumerate(fctl):
		# "width and height ... must be greater than zero."
		ok = w > 0 and h > 0
		if not ok:
			_apng_fault(d, "11.3.6.2#dimensions", f"fcTL {i} frame {w}x{h}")
		# "x_offset plus width must not be greater than the IHDR width; similarly y_offset plus height must not be
		# greater than the IHDR height."
		if x + w > d.width or y + h > d.height:
			_apng_fault(d, "11.3.6.2#region", f"fcTL {i} region {w}x{h}+{x}+{y} outside the {d.width}x{d.height} image")
		# "Valid values for dispose_op are: 0 ... 1 ... 2"; "Valid values for blend_op are: 0 ... 1"
		if dispose > 2:
			_apng_fault(d, "11.3.6.2#dispose-op", f"fcTL {i} dispose_op {dispose}")
		if blend > 1:
			_apng_fault(d, "11.3.6.2#blend-op", f"fcTL {i} blend_op {blend}")
		# "The fcTL chunk corresponding to the default image, if it exists, has these restrictions: The x_offset and
		# y_offset fields must be 0. The width and height fields must equal the corresponding fields from the IHDR chunk."
		if not after and (x or y or w != d.width or h != d.height):
			_apng_fault(d, "11.3.6.2#default-image", f"fcTL {i} for the static image is {w}x{h}+{x}+{y}, not {d.width}x{d.height}+0+0")
		if ok:
			d.reach("11.3.6.2#fctl")
	# 11.3.6.3 "At least one fdAT chunk is required for each frame, except for the first frame, if that frame is
	# represented by an IDAT chunk." An fdAT belongs to the fcTL before it.
	current = None
	for e in d.apng + [("end",)]:
		if e[0] in ("fcTL", "end") and current is not None and current[2] and not current[3]:
			_apng_fault(d, "11.3.6.3#frame-data", f"frame with sequence number {current[1][0]} has no fdAT")
		if e[0] == "fcTL":
			current = [*e, False]
		elif e[0] == "fdAT":
			# The fcTL before IDAT belongs to the static image, whose data is IDAT.
			if current is None or not current[2]:
				_apng_fault(d, "11.3.6.3#needs-fctl", f"fdAT {e[1]} has no preceding fcTL after IDAT")
			else:
				current[3] = True
				d.reach("11.3.6.3#fdat")
	# 4.9.2 "The first fcTL chunk shall contain sequence number 0, and the sequence numbers in the remaining fcTL
	# and fdAT chunks shall be in ascending order, with no gaps or duplicates." 13.1 "Decoders shall treat
	# out-of-order APNG chunks as an error."
	seq = [e[1][0] if e[0] == "fcTL" else e[1] for e in d.apng if e[0] in ("fcTL", "fdAT")]
	if seq == list(range(len(seq))):
		if seq:
			d.reach("4.9.2#sequence-in-order")
	else:
		_apng_fault(d, "4.9.2#sequence-numbers", f"sequence numbers {seq[:12]}{'...' if len(seq) > 12 else ''}")


def _extension(d: _State, f: Values) -> None:
	"""Extension metadata is fully validated by its declared fields; it does not change pixels."""


def _idot(d: _State, f: Values) -> None:
	# Owner's iDOT rule: "segments cover rows 0..height-1 in order, contiguous, each at least one row".
	row = 0
	for segment in f.segments:
		d.expect(segment.first == row and segment.rows > 0, "idot#rows", "iDOT rows are not contiguous nonempty segments")
		row = segment.first + segment.rows
	d.expect(row == d.height, "idot#rows", "iDOT segments do not end at the image height")
	d.idot = (d.chunk_offset, f.segments)
	d.segment_rows = {s.first for s in f.segments[1:]}


class _SegmentObserver:
	"""Validate restart points in the one inflate pass; the observer never changes decoding."""
	def __init__(self, d, points):
		self.d, self.points = d, points
		self.pending = set(points)
		self.floor = 0

	def block(self, bitpos, produced):
		if bitpos % 8 == 0 and bitpos // 8 in self.points:
			position = bitpos // 8
			assert position in self.pending, "inflate cannot revisit a block boundary"
			self.pending.remove(position)
			# Owner: "has produced exactly the bytes of the rows before the segment".
			self.d.expect(produced == self.points[position], "idot#independence", "iDOT boundary has the wrong number of decoded rows")
			self.floor = produced

	def reference(self, produced, distance):
		# Owner: "no LZ77 back-reference resolved at or after that position reaches before it".
		self.d.expect(produced - distance >= self.floor, "idot#independence", "iDOT segment refers to earlier segment data")

	def finish(self):
		# Owner: "at a block boundary, byte-aligned". Missing boundaries also include truncated streams.
		self.d.expect(not self.pending, "idot#independence", "iDOT segment does not start on a byte-aligned DEFLATE block boundary")


def _idot_segments(d: _State):
	# Segments are row ranges of a non-interlaced image; on an interlaced one idot#interlace is the fault.
	if d.idot is None or d.interlace:
		return None
	origin, segments = d.idot
	assert segments and d.idat_offsets, "parsed iDOT and image data must exist before inflate"
	points = {}
	previous = -1
	rowbytes = 1 + (d.width * CHANNELS[d.ctype] * d.depth + 7) // 8
	for k, segment in enumerate(segments):
		offset = origin + segment.offset
		# Owner: "Offsets: increasing; each lands on the start of an IDAT chunk; segment 0 on the first IDAT."
		valid = d.expect(offset > previous and offset in d.idat_offsets and (k != 0 or offset == next(iter(d.idat_offsets))),
			"idot#offsets", f"iDOT segment {k} does not point to its ordered IDAT start")
		previous = offset
		if valid and k:
			position = d.idat_offsets[offset]
			# Empty IDATs cannot create separate restart points with different row counts.
			if d.expect(position not in points, "idot#independence", "iDOT segments share a compressed stream position"):
				points[position] = segment.first * rowbytes
	return _SegmentObserver(d, points)


@dataclass(frozen=True)
class Chunk:
	handler: object
	layout: object


def layout(*fields, clause="13.2#known-length"):
	return Layout(Struct(fields), clause)


def sample_layout(d, clause, mask_clause):
	# 11.3.1.1 / 11.3.4.1: grey = 2 bytes, RGB = 6 bytes, indexed = byte entries.
	count = {0: 1, 2: 3, 3: 1, 4: 1, 6: 3}[d.ctype]
	return layout(("samples", Repeat(BYTE if d.ctype == 3 else Sample(mask_clause), count)), clause=clause)


def palette_layout(d):
	# 11.2.2 "from 1 to 256 palette entries ... length not divisible by 3 is an error".
	id = "11.2.2#invalid-indexed" if d.ctype == 3 else "11.2.2#invalid-truecolour"
	return layout(("entries", Repeat(Struct((("red", BYTE), ("green", BYTE), ("blue", BYTE))),
		minimum=1, maximum=256)), clause=id)


def trns_layout(d):
	if d.ctype == 3:
		return layout(("samples", Repeat(BYTE)), clause="11.3.1.1#length")
	return sample_layout(d, "11.3.1.1#length", "11.3.1.1#mask")


# Chunk layouts and handlers share one registration. UInt(4) unconditionally enforces 7.1;
# signed extension values and Apple uint32 have explicit types. Framing derives from the same declarations.
# 11.2.1: width/height, bit depth, colour type, compression, filter, interlace.
# 15.3.3 "An error is reported if an unrecognized value is encountered" in the five method bytes.
CHUNKS = {
	# ext 4.1: "X position: 4 bytes (signed integer) ... Y position ... Unit specifier: 1 byte";
	# units: "0: unit is the pixel (true dimensions unspecified)", "1: unit is the micrometer".
	b"oFFs": Chunk(_extension, layout(("x", SInt32()), ("y", SInt32()),
		("unit", enum((0, 1), "ext-4.1#unit")), clause="ext-4.1#layout")),
	# ext 4.2: "Calibration name ... Original zero ... Original max ... Equation type ... Number of parameters".
	# The name "is subject to the same restrictions as the keyword in a PNG text chunk".
	b"pCAL": Chunk(_extension, layout(("name", Text("keyword", True, "ext-4.2#name")),
		("x0", SInt32()), ("x1", SInt32()),
		# "they must be different from each other."
		("range", Rule(lambda p: p.values["x0"] != p.values["x1"], "ext-4.2#range", "x0 equals x1")),
		("equation", enum(range(4), "ext-4.2#equation")), ("count", BYTE),
		# "must be correct for the specified equation type" (equations 0, 1, 2, 3 use 2, 3, 3, 4 parameters).
		("arity", Rule(lambda p: p.values["count"] == (2, 3, 3, 4)[p.values["equation"]], "ext-4.2#parameters", "wrong parameter count for equation")),
		# "no null separator ... after the unit name, if there are zero parameters".
		("unit", Choice(lambda p: bool(p.values["count"]), {False: Text("printable", clause="ext-4.2#unit"),
			True: Text("printable", True, "ext-4.2#unit")})),
		("parameters", TextList(Text("float", clause="ext-2.2#float"), lambda p: p.values["count"], "ext-4.2#parameters")),
		clause="ext-4.2#layout")),
	# ext 4.3 "Unit specifier ... Pixel width ... Null separator ... Pixel height"; "Both values ... greater than zero."
	# "The following values are legal for the unit specifier: 1: unit is the meter; 2: unit is the radian".
	b"sCAL": Chunk(_extension, layout(("unit", enum((1, 2), "ext-4.3#unit")),
		("width", Text("float", True, "ext-2.2#float")),
		("positive_width", Rule(lambda p: positive_float(p.values["width"]), "ext-4.3#positive", "pixel width is not positive")),
		("height", Text("float", clause="ext-2.2#float")),
		("positive_height", Rule(lambda p: positive_float(p.values["height"]), "ext-4.3#positive", "pixel height is not positive")),
		clause="ext-4.3#layout")),
	# ext 4.4 "Disposal Method: 1 byte ... User Input Flag: 1 byte ... Delay Time: 2 bytes".
	# GIF89a 23: disposal values 0-3 are defined; "4-7 - To be defined"; User Input Flag is one bit.
	b"gIFg": Chunk(_extension, layout(("disposal", enum(range(4), "ext-4.4#disposal")),
		("input", enum((0, 1), "ext-4.4#input")), ("delay", UINT16), clause="ext-4.4#layout")),
	# ext 4.5 "eight printable ASCII characters"; authentication and application data are application-specific.
	b"gIFx": Chunk(_extension, layout(("identifier", Repeat(UInt(1, "ext-4.5#identifier", range(32, 127)), 8)),
		("authentication", Repeat(BYTE, 3)), ("data", Repeat(BYTE)), clause="ext-4.5#layout")),
	# ext 4.6 "Mode: 1 byte"; "0: cross-fuse layout; 1: diverging-fuse layout".
	b"sTER": Chunk(_extension, layout(("mode", enum((0, 1), "ext-4.6#mode")),
		# "padding := 15 - ((W - 1) mod 16); if (padding > 7) then error".
		("width", Rule(lambda p: 15 - ((p.d.width - 1) % 16) <= 7, "ext-4.6#width", "image width cannot hold a padded stereo pair")),
		clause="ext-4.6#layout")),
	# ext 7.1 "Text Grid Left Position ... Top Position ... Width ... Height ... Character Cell Width ... Height";
	# "Text Foreground Color ... Background Color ... Plain Text Data". Text is a continuous byte stream.
	b"gIFt": Chunk(_extension, layout(("left", SInt32()), ("top", SInt32()), ("width", UINT31), ("height", UINT31),
		("cell_width", BYTE), ("cell_height", BYTE), ("foreground", Repeat(BYTE, 3)), ("background", Repeat(BYTE, 3)),
		# GIF89a 25: "The textual data will be encoded with the 7-bit printable ASCII characters."
		("text", Repeat(UInt(1, "ext-7.1#text", range(32, 127)))), clause="ext-7.1#layout")),
	# Owner's iDOT layout: "uint32 segment count, then per segment (first row, row count, offset)";
	# "count >= 1, length 4 + 12*count". The parser enforces count and complete consumption.
	b"iDOT": Chunk(_idot, layout(("count", AppleUInt32()),
		("nonempty", Rule(lambda p: p.values["count"] >= 1, "idot#layout", "iDOT has no segments")),
		("segments", Repeat(Struct((("first", AppleUInt32()), ("rows", AppleUInt32()), ("offset", AppleUInt32()))), lambda p: p.values["count"])),
		# Owner: "image not interlaced".
		("interlace", Rule(lambda p: not p.d.interlace, "idot#interlace", "iDOT image is interlaced")),
		clause="idot#layout")),
	b"IHDR": Chunk(_ihdr, layout(
		("width", UInt(4, "11.2.1#dimensions-range")), ("height", UInt(4, "11.2.1#dimensions-range")),
		("depth", enum((1, 2, 4, 8, 16), "11.2.1#bit-depth")),
		("colour", enum(CHANNELS, "11.2.1#colour-type")),
		("compression", enum((0,), "11.2.1#compression-method")),
		("filter", enum((0,), "11.2.1#filter-method")),
		("interlace", enum((0, 1), "11.2.1#interlace-method")), clause="11.2.1#length")),
	b"PLTE": Chunk(_plte, palette_layout),
	b"IDAT": Chunk(_idat, layout(("stream", Fragment()))),
	b"IEND": Chunk(_iend, layout(("extra", RemainderLength()))),
	b"tRNS": Chunk(_trns, trns_layout),
	# 11.3.2.1: "White Point x ... Blue y" (eight four-byte values); 11.3.2.2: "Gamma 4 bytes".
	b"cHRM": Chunk(_chrm, layout(("points", Repeat(UINT31, 8)))),
	b"gAMA": Chunk(_gama, layout(("gamma", UINT31))),
	# 11.3.2.3: "Profile name ... Null separator ... Compression method ... Compressed profile".
	# "The only compression method defined ... is method 0".
	b"iCCP": Chunk(_iccp, layout(("name", Text("keyword", True)),
		("method", enum((0,), "11.3.2.3#method")), ("profile", Compressed(Content("ICC"), "11.3.2.3#zlib")),
		clause="11.3.2.3#separator")),
	b"sBIT": Chunk(_sbit, layout(("depths", Repeat(BYTE, lambda p: {0: 1, 2: 3, 3: 3, 4: 2, 6: 4}[p.d.ctype])))),
	# 11.3.2.5 rendering intent: "Only the following values shall be used" (0..3).
	b"sRGB": Chunk(_srgb, layout(("intent", enum(range(4), "11.3.2.5#intent")))),
	# 11.3.2.6 "Each of the fields of the cICP chunk corresponds to the parameter of the same name in [ITU-T-H.273]";
	# H.273 (07/2024) 3.6: reserved code points "shall not be used in identifiers conforming to this version".
	# "Matrix Coefficients shall be set to 0"; H.273 VideoFullRangeFlag is a one-bit flag.
	b"cICP": Chunk(_cicp, layout(
		("primaries", enum((1, 2, *range(4, 13), 22), "11.3.2.6#primaries")),  # H.273 Table 2
		("transfer", enum((1, 2, *range(4, 19)), "11.3.2.6#transfer")),  # H.273 Table 3
		("matrix", enum((0,), "11.3.2.6#matrix")), ("full_range", enum((0, 1), "11.3.2.6#full-range")))),
	# 11.3.2.7: eight two-byte chromaticities, then two four-byte luminances.
	b"mDCV": Chunk(_mdcv, layout(("chromaticities", Repeat(UINT16, 8)), ("maximum", UINT31), ("minimum", UINT31))),
	# 11.3.2.8: "MaxCLL 4 bytes ... MaxFALL 4 bytes".
	b"cLLI": Chunk(_clli, layout(("max_cll", UINT31), ("max_fall", UINT31))),
	# 11.3.3.2 "The keyword and text string are separated by a zero byte (null character)."
	b"tEXt": Chunk(_text, layout(("keyword", Text("keyword", True)),
		("text", Text("latin1", clause="11.3.3.2#latin1", null_clause="11.3.3.2#null")), clause="11.3.3.2#separator")),
	# 11.3.3.3: "The only value defined ... is 0 (deflate compression)."
	b"zTXt": Chunk(_ztxt, layout(("keyword", Text("keyword", True)), ("method", enum((0,), "11.3.3.3#method")),
		("text", Compressed(Text("latin1", clause="11.3.3.3#latin1", null_clause="11.3.3.3#null"), "11.3.3.3#zlib")),
		clause="11.3.3.3#separator")),
	# 11.3.3.4 "compression flag is 0 for uncompressed text, 1 for compressed text".
	# "For uncompressed text ... decoders shall ignore" the method; compressed method is 0.
	b"iTXt": Chunk(_itxt, layout(("keyword", Text("keyword", True)), ("flag", enum((0, 1), "11.3.3.4#flag")),
		("method", Choice(lambda p: p.values["flag"], {0: BYTE, 1: enum((0,), "11.3.3.4#method")})),
		("language", Text("language", True)), ("translated", Text("utf8", True, "11.3.3.4#utf8", "11.3.3.4#null")),
		("text", Compressed(Text("utf8", clause="11.3.3.4#utf8", null_clause="11.3.3.4#null"), "11.3.3.4#zlib", "flag")),
		clause="11.3.3.4#separators")),
	b"bKGD": Chunk(_bkgd, lambda d: sample_layout(d, "13.2#known-length", "11.3.4.1#mask")),
	# 11.3.4.2 "exactly one entry for each entry in the PLTE chunk".
	b"hIST": Chunk(_hist, layout(("frequencies", Repeat(UINT16, lambda p: len(p.d.plte))), clause="11.3.4.2#entries")),
	# 11.3.4.3 unit: "0 unit is unknown, 1 unit is the metre".
	b"pHYs": Chunk(_phys, layout(("x", UINT31), ("y", UINT31), ("unit", enum((0, 1), "11.3.4.3#unit")))),
	# 11.3.4.4 "sample depth shall be 8 or 16"; "divisible by 6 ... or by 10".
	b"sPLT": Chunk(_splt, layout(("name", Text("keyword", True)), ("depth", enum((8, 16), "11.3.4.4#depth")),
		("entries", Choice(lambda p: p.values["depth"], {
			depth: Repeat(Struct((("red", sample), ("green", sample), ("blue", sample), ("alpha", sample), ("frequency", UINT16))),
				clause="11.3.4.4#length") for depth, sample in ((8, BYTE), (16, UINT16))})), clause="11.3.4.4#separator")),
	b"eXIf": Chunk(_exif, layout(("profile", Content("Exif")))),
	# 11.3.5.1 "Month ... (1-12), Day ... (1-31), Hour ... (0-23), Minute ... (0-59), Second ... (0-60)".
	b"tIME": Chunk(_time, layout(("year", UINT16), ("month", enum(range(1, 13), "11.3.5.1#range")),
		("day", enum(range(1, 32), "11.3.5.1#range")), ("hour", enum(range(24), "11.3.5.1#range")),
		("minute", enum(range(60), "11.3.5.1#range")), ("second", enum(range(61), "11.3.5.1#range")))),
	# 11.3.6.1 "num_frames ... num_plays"; 11.3.6.2 frame fields in table order.
	b"acTL": Chunk(_actl, layout(("frames", UINT31), ("plays", UINT31), clause="11.3.6.1#length")),
	b"fcTL": Chunk(_fctl, layout(("sequence", UINT31), ("width", UINT31), ("height", UINT31),
		("x", UINT31), ("y", UINT31), ("delay_num", UINT16), ("delay_den", UINT16),
		("dispose", BYTE), ("blend", BYTE), clause="11.3.6.2#length")),
	# 11.3.6.3 "Sequence number 4 bytes ... Frame data n bytes".
	b"fdAT": Chunk(_fdat, layout(("sequence", UINT31), ("stream", Fragment()), clause="11.3.6.3#length")),
}


# ---------------------------------------------------------------- Output: RGBA

def _output(d: _State, rows: list[list[int]]) -> Image:
	"""Expand to RGBA at 8 bits (bit depth 8 or less) or 16 bits."""
	maxval = 65535 if d.depth == 16 else 255
	flat = [v for row in rows for v in row]
	n = d.width * d.height
	ct = d.ctype
	if ct == 3:
		alpha = d.trns or b""
		lut = [(*d.plte[i], alpha[i] if i < len(alpha) else 255) if i < len(d.plte) else (0, 0, 0, 255) for i in range(256)]
		if any(lut[v][3] < 255 for v in set(flat)):
			d.reach("11.3.1.1#applied")
		return Image(d.width, d.height, maxval, array("B", b"".join(bytes(lut[v]) for v in flat)), d.diagnostics)
	nch = CHANNELS[ct]
	if ct in (0, 2) and d.trns is not None:
		# 11.3.1.1 "Pixels of the specified grey sample value or RGB sample values are treated as transparent
		# (equivalent to alpha value 0); all other pixels are to be treated as fully opaque"
		key = d.trns
		alpha = [0 if tuple(flat[i:i + nch]) == key else maxval for i in range(0, len(flat), nch)] if nch > 1 \
			else [0 if v == key[0] else maxval for v in flat]
		if 0 in alpha:
			d.reach("11.3.1.1#applied")
	elif ct in (4, 6):
		alpha = flat[nch - 1::nch]
	else:
		alpha = [maxval] * n
	if d.depth < 8:
		# Sub-byte greyscale to 8 bits: v * 255 / (2^depth - 1) is exact and equals left bit replication (13.12).
		scale = 255 // ((1 << d.depth) - 1)
		flat = [v * scale for v in flat]
	out = [0] * (4 * n)
	if ct in (0, 4):
		grey = flat[0::nch]
		out[0::4] = out[1::4] = out[2::4] = grey
	else:
		for c in range(3):
			out[c::4] = flat[c::nch]
	out[3::4] = alpha
	return Image(d.width, d.height, maxval, array("H" if maxval > 255 else "B", out), d.diagnostics)


def to_pam(img: Image) -> bytes:
	head = (f"P7\nWIDTH {img.width}\nHEIGHT {img.height}\nDEPTH 4\nMAXVAL {img.maxval}\n"
		"TUPLTYPE RGB_ALPHA\nENDHDR\n").encode()
	samples = img.samples
	if samples.typecode == "H" and sys.byteorder == "little":
		samples = array("H", samples)
		samples.byteswap()
	return head + samples.tobytes()


def main(argv: list[str]) -> int:
	if argv == ["--version"]:
		print(VERSION)
		return 0
	edition = 3
	if argv[:1] == ["--edition"]:
		edition = int(argv[1])
		argv = argv[2:]
	if len(argv) != 2 or edition not in (2, 3):
		print("usage: refpng.py [--edition 2|3] in.png out.pam | --version", file=sys.stderr)
		return 2
	with open(argv[0], "rb") as f:
		data = f.read()
	record = Diagnostics()
	try:
		img = decode(data, edition, diagnostics=record)
		with open(argv[1], "wb") as f:
			f.write(to_pam(img))
	except Reject:
		return 1
	finally:
		# The same accumulated record is emitted for success, Reject, MemoryError or a programming error.
		# Unexpected exceptions propagate with a traceback and no OK line.
		record.emit()
	print(f"OK {img.width} {img.height} 4 {16 if img.maxval > 255 else 8}")
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
