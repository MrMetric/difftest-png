"""The synthesized test files: picture.py's picture in every file, each file testing one thing.

	uv run synth.py [DIR]       # DIR defaults to out/synth/

Deletes every *.png in DIR, writes each case and DIR/index.tsv (name, expect3, clause, note), and prints
the count. Every file shows the picture at 13x15 as 8-bit RGB, non-interlaced, filter None, zlib level 6,
unless the test is about one of those: then it uses the colour type, depth or size the test needs, and
a smaller size is the picture's top-left crop. Files are as directly comparable as possible outside
the trait being tested, so most decode to the identical image.

Every file goes through emit(), which reads it with the reference exactly as expect.py does
(expect.reading) before writing it, and asserts:
- lens 3's expectation (pnglens.STRENGTH) is the case's expect3;
- every clause the case names (`clause`, comma-joined ids of spec/clauses.tsv) is reached;
- the preferred interpretation (pnglens.preferred) has exactly the expected pixels as RGBA16: the
  variant's own by default, or the pixels the fault produces on purpose (a palette index past PLTE is
  opaque black; a tRNS value is alpha 0); a case expecting no pixels has no interpretation at all;
- no two files have the same name or the same bytes.
Randomness comes only from random.Random seeded with the case name, so two runs write identical bytes.
The external DEFLATE encoders (zopfli, libdeflate-gzip, 7z) are optional: an absent one, or one whose
stream equals an earlier one's, is noted on stderr and its file is not written.
"""
import functools
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter
from dataclasses import replace
from pathlib import Path

import numpy as np

import deflate as D
import expect
import picture
import pnglens
import pngbuild as B
from picture import Variant

ROOT = Path(__file__).resolve().parent
NAME = re.compile(r"[a-z0-9.-]+")
VARIANTS = {(ct, bd): picture.variant(ct, bd) for ct, bd in picture.COMBOS}
TAGS = {v.name: v for v in VARIANTS.values()}
RGB8, RGB16, G8, GA8, RGBA8, P8 = (TAGS[t] for t in ("rgb8", "rgb16", "g8", "ga8", "rgba8", "p8"))
IEND = B.chunk(b"IEND")
GAMA = struct.pack(">I", 45455)
SRGB_CHRM = struct.pack(">8I", 31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000)
# The smallest ICC profile the reference reads as one (ICC.1 7.2: exact size, 'acsp'; 7.3: an empty tag table), RGB,
# for the iCCP tests that are not about the profile itself.
ICC_RGB = struct.pack(">I", 132) + bytes(12) + b"RGB " + bytes(16) + b"acsp" + bytes(88) + struct.pack(">I", 0)
assert len(ICC_RGB) == 132


# ---------------------------------------------------------------- variants and their pixels

def crop(v: Variant, width: int, height: int) -> Variant:
	"""The top-left width x height of v: the same samples and palette, since the picture is quantized per pixel."""
	nch = B.CHANNELS[v.colour_type]
	c = picture.variant(v.colour_type, v.bit_depth, width, height)
	assert c == replace(v, width=width, height=height, rows=tuple(r[:width * nch] for r in v.rows[:height])), (v.name, width, height)
	return c


def entries(v: Variant) -> tuple[tuple[int, int, int], ...]:
	assert v.palette is not None, v.name
	return v.palette


def plte_body(v: Variant) -> bytes:
	return bytes(s for entry in entries(v) for s in entry)


def samples(v: Variant) -> list[list[tuple[int, ...]]]:
	"""Each pixel's samples, grouped per pixel."""
	nch = B.CHANNELS[v.colour_type]
	return [[row[i * nch:(i + 1) * nch] for i in range(v.width)] for row in v.rows]


def pixels(v: Variant, *, trns: bytes | None = None, palette: tuple[tuple[int, int, int], ...] | None = None) -> list[list[tuple[int, ...]]]:
	"""The RGBA16 pixels a decoder produces for v's samples with tRNS body `trns` applied (11.3.1.1: masked to
	the bit depth; alpha 0 where a pixel matches; palette alpha per entry) and, for an indexed image,
	`palette` instead of v's (13.1: an index past the last entry is opaque black)."""
	out = v.rgba16()
	if v.colour_type == 3:
		pal = entries(v) if palette is None else palette
		alpha = list(trns or b"")
		out = [[(0, 0, 0, 65535) if i >= len(pal) else (pal[i][0] * 257, pal[i][1] * 257, pal[i][2] * 257,
			alpha[i] * 257 if i < len(alpha) else 65535) for (i,) in row] for row in samples(v)]
		if palette is None and trns is None:
			assert out == v.rgba16()
		return out
	assert palette is None
	if trns is None:
		return out
	assert v.colour_type in (0, 2), "tRNS applies to greyscale and truecolour only"
	mask = (1 << v.bit_depth) - 1
	key = tuple(s & mask for s in struct.unpack(f">{len(trns) // 2}H", trns))
	assert len(key) == B.CHANNELS[v.colour_type], trns
	return [[(r, g, b, 0) if s == key else (r, g, b, a) for (r, g, b, a), s in zip(prow, srow)]
		for prow, srow in zip(out, samples(v))]


def occurs(v: Variant, value: tuple[int, ...]) -> bool:
	return any(s == value for row in samples(v) for s in row)


def mid(v: Variant) -> tuple[int, ...]:
	"""The most frequent pixel whose samples all lie strictly between 0 and the maximum (at 16 bits, samples
	whose two bytes differ), first in raster order on a tie."""
	top = (1 << v.bit_depth) - 1
	def ok(s: int) -> bool:
		return 0 < s < top and (v.bit_depth != 16 or s >> 8 != s & 0xFF)
	counts = Counter(s for row in samples(v) for s in row if all(map(ok, s)))
	assert counts, v.name
	return counts.most_common(1)[0][0]


def trns_for(value: tuple[int, ...]) -> bytes:
	return struct.pack(f">{len(value)}H", *value)


# ---------------------------------------------------------------- datastreams

@functools.cache
def raw(v: Variant, ftype: int = 0) -> bytes:
	"""v serialized non-interlaced with filter `ftype` on every scanline."""
	return B.serialize(v.rows, v.width, v.height, v.colour_type, v.bit_depth, filter_for=lambda p, y: ftype)


@functools.cache
def zdata(v: Variant) -> bytes:
	return zlib.compress(raw(v), 6)


def image(v: Variant, **kw) -> bytes:
	"""pngbuild.build for v, with v's palette as PLTE unless `plte` is given."""
	if v.palette is not None:
		kw.setdefault("plte", plte_body(v))
	return B.build(v.width, v.height, v.bit_depth, v.colour_type, v.rows, **kw)


def png(v: Variant = RGB8, *, ihdr: bytes | None = None, before=(), plte: list[bytes] | None = None, after_plte=(),
		z: bytes | None = None, idats: list[bytes] | None = None, after_idat=(), iend: bytes | None = IEND,
		trailer: bytes = b"") -> bytes:
	"""v as IHDR, `before`, PLTE (v's own when `plte` is None; the chunks given otherwise, [] for none),
	`after_plte`, IDAT (one chunk of `z`, default zlib level 6 of filter None, or the chunks `idats`),
	`after_idat`, `iend` (None for none), then `trailer` bytes."""
	parts = [ihdr or B.ihdr(v.width, v.height, v.bit_depth, v.colour_type)] + list(before)
	if plte is None:
		plte = [B.chunk(b"PLTE", plte_body(v))] if v.palette is not None else []
	parts += plte + list(after_plte)
	parts += [B.chunk(b"IDAT", zdata(v) if z is None else z)] if idats is None else idats
	parts += list(after_idat) + ([] if iend is None else [iend])
	return B.assemble(parts) + trailer


def zlib_png(z: bytes, v: Variant = RGB8) -> bytes:
	return png(v, z=z)


# ---------------------------------------------------------------- emit

class Corpus:
	"""The files written so far, and the reference check every one passes before it is written."""

	def __init__(self, out: Path, scratch: Path):
		self.out, self.scratch = out, scratch
		self.index: list[tuple[str, str, str, str]] = []
		self.names: set[str] = set()
		self.contents: dict[bytes, str] = {}

	def emit(self, name: str, data: bytes, expect3: str, clause: str, note: str, pixels: Variant | list | None = RGB8) -> expect.Reading:
		"""Check the file against the reference and write it. `clause`: comma-joined clause ids the case is about,
		or "". `pixels`: a Variant (its rgba16()), RGBA16 rows, or None for a file without reference pixels."""
		assert NAME.fullmatch(name), f"{name}: not a [a-z0-9.-]+ name"
		assert name not in self.names, f"{name}: duplicate"
		assert data not in self.contents, f"{name}: the same bytes as {self.contents.get(data)}"
		assert expect3 in pnglens.STRENGTH, (name, expect3)
		assert note and "\t" not in note and "\n" not in note, (name, note)
		tmp = self.scratch / f"{name}.png"
		tmp.write_bytes(data)
		r = expect.reading(tmp)
		tmp.unlink()
		v3 = r.verdicts["3"]
		said = pnglens.faults_in_words(v3) or "no fault"
		assert v3.expectation == expect3, f"{name}: lens 3 expects {v3.expectation}, the case says {expect3}: {said}"
		for id in filter(None, clause.split(",")):
			assert id in pnglens.CLAUSES, f"{name}: unknown clause {id}"
			assert id in r.diagnostics.reached, f"{name}: does not reach {id}; reaches {sorted(r.diagnostics.reached)}"
		if pixels is None:
			assert not r.interpretations, f"{name}: no pixels expected, the reference has {len(r.interpretations)} interpretations: {said}"
		else:
			assert r.interpretations, f"{name}: pixels expected, the reference has none: {said}"
			want = np.array(pixels.rgba16() if isinstance(pixels, Variant) else pixels, dtype=np.uint16)
			got = pnglens.preferred(r.interpretations).rgba
			assert got.shape == want.shape, f"{name}: the reference decodes {got.shape}, expected {want.shape}"
			wrong = np.argwhere((got != want).any(axis=2))
			assert not len(wrong), (f"{name}: {len(wrong)} pixels differ from the expected ones; first at (x, y) = "
				f"({wrong[0][1]}, {wrong[0][0]}): {got[tuple(wrong[0])].tolist()} instead of {want[tuple(wrong[0])].tolist()}")
		(self.out / f"{name}.png").write_bytes(data)
		self.names.add(name)
		self.contents[data] = name
		self.index.append((name, expect3, clause, note))
		return r


# ---------------------------------------------------------------- 9 filters

# Combos at which no Paeth tie between predictors of different value occurs in the picture: their
# filter-paeth file does not exercise the comparison order (9.4#paeth-order).
NO_DECIDING_TIE = frozenset({"g1", "g2", "g4", "g8", "p1", "p2", "p4", "ga8"})


def filters(c: Corpus) -> None:
	for (ct, bd), v in VARIANTS.items():
		deep = ct != 3 and bd >= 8  # sample bytes reach past 127, so an Average sum can carry and Paeth can pick c
		for f, fname in enumerate(B.FILTERS):
			s: dict[str, int] = {}
			data = image(v, filter_for=lambda p, y, f=f: f, stats=s)
			n = {k: s.get(k, 0) for k in B.STATS}
			match fname:
				case "none":
					note = f"every scanline filter None: the plain {v.name} picture"
				case "sub" | "up":
					assert n["wrap"] > 0, (v.name, fname, n)
					note = f"every scanline filter {fname.title()}; {n['wrap']} bytes are below their prediction (the subtraction wraps)"
				case "average":
					assert n["avg_odd"] > 0 and (n["avg_carry"] > 0 or not deep), (v.name, n)
					note = f"every scanline filter Average; a + b is odd at {n['avg_odd']} bytes and exceeds 255 at {n['avg_carry']}"
				case "paeth":
					assert n["paeth_a"] > 0 and n["paeth_b"] > 0 and (n["paeth_c"] > 0 or not deep), (v.name, n)
					assert (n["paeth_tie"] > 0) == (v.name not in NO_DECIDING_TIE), (v.name, n)
					note = (f"every scanline filter Paeth; the predictor is a at {n['paeth_a']} bytes, b at {n['paeth_b']}, "
						f"c at {n['paeth_c']}, and the tie order decides {n['paeth_tie']}")
				case _:
					raise AssertionError(fname)
			if fname != "none":
				note += "; the first scanline sees a zero previous scanline"
			r = c.emit(f"filter-{fname}-{v.name}", data, "display", f"9.2#filter-{fname}", note, v)
			reached = r.diagnostics.reached
			# The reference's own coverage must agree with what the encoder counted.
			if fname == "average":
				assert (n["avg_carry"] > 0) == ("9.3#average-overflow" in reached), (v.name, n)
			if fname == "paeth":
				assert (n["paeth_tie"] > 0) == ("9.4#paeth-order" in reached), (v.name, n)
		name = f"filter-mix-{v.name}"
		order = [f for f in range(5) for _ in range(3)]
		random.Random(name).shuffle(order)
		assert len(order) == v.height
		c.emit(name, image(v, filter_for=lambda p, y, o=order: o[y]), "display", ",".join(f"9.2#filter-{f}" for f in B.FILTERS),
			f"filter type per scanline, each type on three scanlines in a seeded random order: {''.join(map(str, order))}", v)
		t = crop(v, 1, 1)
		c.emit(f"tiny-1x1-{v.name}", image(t, filter_for=lambda p, y: 4), "display", f"11.2.1#{v.name}",
			"the picture's top-left pixel alone, Paeth filtered", t)


# ---------------------------------------------------------------- 8.1 Adam7

ADAM7_CROPS = ((1, 1), (1, 2), (2, 1), (3, 3), (4, 4), (5, 5), (1, 9), (9, 1), (2, 7), (7, 2), (8, 9))


def empty_passes(w: int, h: int) -> tuple[list[int], list[str]]:
	"""The 1-based passes with no scanlines, and the 9.1 clauses the reference reaches for them."""
	empty, clauses = [], []
	for p in range(7):
		pw, ph = B.pass_size(w, h, p)
		if ph == 0:
			empty.append(p + 1)
			clauses.append("9.1#pass-without-rows")
		elif pw == 0:
			empty.append(p + 1)
			clauses.append("9.1#empty-pass")
	return empty, list(dict.fromkeys(clauses))


def adam7(c: Corpus) -> None:
	assert empty_passes(picture.W, picture.H) == ([], []) and all(min(B.pass_size(picture.W, picture.H, p)) >= 2 for p in range(7))
	for v in VARIANTS.values():
		for size in [None] + list(ADAM7_CROPS):
			t = v if size is None else crop(v, *size)
			name = f"adam7-{v.name}" if size is None else f"adam7-{t.width}x{t.height}-{v.name}"
			rng = random.Random(name)
			empty, clauses = empty_passes(t.width, t.height)
			c.emit(name, image(t, interlace=1, filter_for=lambda p, y: rng.randrange(5)), "display", ",".join(["8.1#adam7"] + clauses),
				f"{t.width}x{t.height}, random filter per scanline; empty passes: {', '.join(map(str, empty)) or 'none'}", t)
	for size in [None] + list(ADAM7_CROPS):
		t = G8 if size is None else crop(G8, *size)
		name = "adam7-g8-nofilter" if size is None else f"adam7-{t.width}x{t.height}-g8-nofilter"
		empty, clauses = empty_passes(t.width, t.height)
		c.emit(name, image(t, interlace=1), "display", ",".join(["8.1#adam7"] + clauses),
			f"{t.width}x{t.height}, filter None throughout: the pass layout alone; empty passes: {', '.join(map(str, empty)) or 'none'}", t)


# ---------------------------------------------------------------- 7.2 packing

def packing(c: Corpus) -> None:
	for tag in ("g1", "g2", "g4", "p1", "p2", "p4"):
		v = TAGS[tag]
		assert v.width * v.bit_depth % 8, tag
		c.emit(f"padbits-{tag}", image(v, pad_bits=1, filter_for=lambda p, y: 1), "display", "7.2#padding-unspecified",
			f"{8 - v.width * v.bit_depth % 8} unused bits at the end of every scanline set to 1, Sub filter so they feed the filter", v)
	g1 = TAGS["g1"]
	assert all(B.pass_size(g1.width, g1.height, p)[0] % 8 for p in range(7)), "every pass pads"
	c.emit("padbits-g1-adam7", image(g1, pad_bits=1, interlace=1, filter_for=lambda p, y: 4), "display", "8.1#adam7,7.2#padding-unspecified",
		"unused bits set to 1 in every reduced image (13 wide, so every pass pads), Paeth filter", g1)


# ---------------------------------------------------------------- 10, 11.2.3 IDAT structure and the zlib stream

def structure(c: Corpus) -> None:
	raw0, z = raw(RGB8), zdata(RGB8)
	row = B.row_bytes(RGB8.width, 2, 8) + 1
	assert row == 40 and len(raw0) == row * RGB8.height
	idat = lambda d: B.chunk(b"IDAT", d)
	c.emit("idat-empty-first", png(idats=[idat(b""), idat(z)]), "display", "10.2#idat-empty", "zero-length IDAT before the data")
	c.emit("idat-empty-last", png(idats=[idat(z), idat(b"")]), "display", "10.2#idat-empty", "zero-length IDAT after the data")
	c.emit("idat-empty-middle", png(idats=[idat(z[:5]), idat(b""), idat(z[5:])]), "display", "10.2#idat-empty", "zero-length IDAT between two halves")
	c.emit("idat-split-every-byte", png(idats=[idat(z[i:i + 1]) for i in range(len(z))]), "display", "10.2#idat-boundaries",
		f"one byte per IDAT, {len(z)} IDAT chunks (8-bit RGB; PngSuite oi9 is 16-bit)")
	c.emit("idat-split-in-zlib-header", png(idats=[idat(z[:1]), idat(z[1:])]), "display", "10.2#split-zlib-header", "IDAT boundary between the two zlib header bytes")
	c.emit("idat-split-in-adler", png(idats=[idat(z[:-2]), idat(z[-2:])]), "display", "10.2#split-adler32", "IDAT boundary inside the Adler-32")
	c.emit("idat-interrupted-by-text", png(idats=[idat(z[:8]), B.chunk(b"tEXt", b"k\0v"), idat(z[8:])]), "must-reject", "11.2.3#consecutive",
		"tEXt between two IDATs: IDATs shall be consecutive")
	c.emit("idat-interrupted-by-unknown", png(idats=[idat(z[:8]), B.chunk(b"juNk", bytes(4)), idat(z[8:])]), "must-reject", "11.2.3#consecutive",
		"unknown ancillary chunk between two IDATs")
	c.emit("idat-after-iend", png(trailer=idat(z)), "recover", "11.2#iend-last", "a second IDAT after IEND: data after IEND")
	c.emit("idat-second-stream", png(idats=[idat(z), idat(z)]), "recover", "11.2.3#trailing-bytes",
		"a second complete zlib stream in a further IDAT (Pillow's hopper_idat_after_image_end)")
	c.emit("idat-trailing-junk", zlib_png(z + b"\x5a\xa5\x00\xff" * 4), "recover", "11.2.3#trailing-bytes",
		"16 unused bytes after the zlib stream in the final IDAT: decoder should ignore")
	c.emit("idat-trailing-zeros", zlib_png(z + bytes(64)), "recover", "11.2.3#trailing-bytes", "64 zero bytes after the zlib stream")
	c.emit("zlib-extra-row", zlib_png(zlib.compress(raw0 + raw0[:row], 6)), "may-reject", "13.1#data-long",
		"decompressed data one scanline longer than the image")
	c.emit("zlib-extra-byte", zlib_png(zlib.compress(raw0 + b"\0", 6)), "may-reject", "13.1#data-long", "decompressed data one byte longer than the image")
	c.emit("zlib-short-row", zlib_png(zlib.compress(raw0[:-row], 6)), "may-reject", "13.1#data-short",
		"decompressed data one scanline short: unexpected end of image data", None)
	c.emit("zlib-short-byte", zlib_png(zlib.compress(raw0[:-1], 6)), "may-reject", "13.1#data-short", "decompressed data one byte short", None)
	c.emit("zlib-empty", zlib_png(zlib.compress(b"", 6)), "may-reject", "13.1#data-short", "a valid zlib stream that decompresses to nothing", None)
	c.emit("zlib-no-adler", zlib_png(z[:-4]), "may-reject", "10.1#zlib-check,rfc1950-2.2#end-in-adler32", "deflate complete, Adler-32 absent")
	c.emit("zlib-bad-adler", zlib_png(z[:-4] + bytes(4)), "must-reject", "rfc1950-2.3#adler32", "Adler-32 wrong, deflate data intact")
	co = zlib.compressobj(6)
	c.emit("zlib-no-final-block", zlib_png(co.compress(raw0) + co.flush(zlib.Z_SYNC_FLUSH)), "may-reject", "10.1#zlib-check,rfc1951-3.2.3#end-of-input",
		"all data in non-final blocks, then the stream stops (no BFINAL, no Adler-32)")
	dyn = D.deflate_hand(raw0, "dynamic")
	c.emit("zlib-cinfo-8", zlib_png(D.zlib_wrap(dyn, raw0, cmf=0x88)), "must-reject", "10.1#zlib-method",
		"zlib header claims a 64 KiB window (CINFO 8); PNG allows at most 32768", None)
	c.emit("zlib-cinfo-0", zlib_png(D.zlib_wrap(dyn, raw0, cmf=0x08)), "display", "rfc1950-2.2#cinfo-small",
		"zlib header claims a 256-byte window; the data uses no distance at all")
	c.emit("zlib-fdict", zlib_png(D.zlib_wrap(dyn, raw0, fdict=b"\x12\x34\x56\x78")), "must-reject", "10.1#no-preset-dictionary",
		"FDICT set with dictionary id 0x12345678; PNG forbids preset dictionaries; the data needs none (an id of 1 would be the Adler-32 of an empty dictionary and some inflaters accept it)", None)
	c.emit("zlib-cm-7", zlib_png(D.zlib_wrap(dyn, raw0, cmf=0x77, flevel=0)), "must-reject", "10.1#zlib-method", "zlib CM 7 instead of 8", None)
	assert z[:2] == b"\x78\x9c", z[:2]
	c.emit("zlib-fcheck-bad", zlib_png(b"\x78\x9d" + z[2:]), "must-reject", "rfc1950-2.2#fcheck", "zlib header check bits wrong (0x789D), data intact", None)
	long_distance(c)


def long_distance(c: Corpus) -> None:
	"""rgb16 written with a window declared as 256 bytes, and matches 316 bytes back."""
	v = RGB16
	r = raw(v)
	row = B.row_bytes(v.width, 2, 16) + 1
	assert row == 79
	last, above = r[(v.height - 1) * row:], r[(v.height - 5) * row:(v.height - 4) * row]
	dist = 4 * row
	assert dist > 256
	# Literals up to the last scanline; there, a match of distance 4 * 79 wherever it agrees with the scanline
	# four above (the ground repeats; only the rock's pixels differ), literals elsewhere.
	tokens: list[tuple] = [("lit", b) for b in r[:(v.height - 1) * row]]
	i = 0
	while i < row:
		j = i
		while j < row and last[j] == above[j]:
			j += 1
		if j - i >= 3:
			tokens.append(("match", j - i, dist))
			i = j
		else:
			tokens.append(("lit", last[i]))
			i += 1
	matched = sum(t[1] for t in tokens if t[0] == "match")
	assert matched >= row // 2 and D.detokenize(tokens) == r, matched
	w = D.BitWriter()
	D.dynamic_block(w, tokens, True)
	z = D.zlib_wrap(w.bytes(), r, cmf=0x08)
	assert zlib.decompress(z) == r
	c.emit("zlib-cinfo-0-long-distance", zlib_png(z, v), "may-reject", "rfc1950-2.2#window-exceeded",
		f"rgb16; window declared 256 bytes (CINFO 0), and {matched} bytes of the last scanline are matches {dist} bytes back (the ground repeats)", v)


# ---------------------------------------------------------------- 10, RFC 1951 DEFLATE

def gzip_to_zlib(gz: bytes, raw_: bytes) -> bytes:
	"""Re-frame a gzip member's DEFLATE data as zlib."""
	assert gz[:2] == b"\x1f\x8b" and gz[2] == 8, gz[:3]
	flg = gz[3]
	pos = 10
	if flg & 4:
		pos += 2 + struct.unpack("<H", gz[pos:pos + 2])[0]
	for bit in (8, 16):
		if flg & bit:
			pos = gz.index(b"\0", pos) + 1
	if flg & 2:
		pos += 2
	return D.zlib_wrap(gz[pos:-8], raw_)


def external(tool: str, raw_: bytes) -> bytes | None:
	"""zlib stream for `raw_` from another encoder, or None when the tool is absent."""
	if shutil.which(tool) is None:
		return None
	with tempfile.TemporaryDirectory() as td:
		src = Path(td) / "raw.bin"
		src.write_bytes(raw_)
		match tool:
			case "zopfli":
				return subprocess.run(["zopfli", "--zlib", "-c", str(src)], capture_output=True, check=True).stdout
			case "libdeflate-gzip":
				return gzip_to_zlib(subprocess.run(["libdeflate-gzip", "-12", "-c", str(src)], capture_output=True, check=True).stdout, raw_)
			case "7z":
				out = Path(td) / "out.gz"
				subprocess.run(["7z", "a", "-tgzip", "-mx=9", "-bso0", "-bse0", str(out), str(src)], check=True)
				return gzip_to_zlib(out.read_bytes(), raw_)
	raise AssertionError(tool)


def deflate(c: Corpus) -> None:
	r = raw(RGB8)
	row = B.row_bytes(RGB8.width, 2, 8) + 1
	wrap = lambda w: zlib_png(D.zlib_wrap(w.bytes(), r))
	c.emit("deflate-stored", zlib_png(zlib.compress(r, 0)), "display", "rfc1951-3.2.4#stored", "zlib level 0: stored blocks")
	c.emit("deflate-fixed", zlib_png(D.zlib_wrap(D.deflate_hand(r, "fixed"), r)), "display", "rfc1951-3.2.6#fixed",
		"single fixed-Huffman block with distance-1 matches")
	for strategy, name, clause, what in ((zlib.Z_HUFFMAN_ONLY, "deflate-huffman-only", "rfc1951-3.2.7#dynamic", "Z_HUFFMAN_ONLY: dynamic blocks without matches"),
			(zlib.Z_RLE, "deflate-rle", "rfc1951-3.2.3#overlap", "Z_RLE: distance-1 matches only"),
			(zlib.Z_FIXED, "deflate-zlib-fixed", "rfc1951-3.2.6#fixed", "Z_FIXED: fixed-Huffman blocks with real matches")):
		co = zlib.compressobj(9, zlib.DEFLATED, 15, 9, strategy)
		c.emit(name, zlib_png(co.compress(r) + co.flush()), "display", clause, f"zlib {what}")
	for flush, name, what in ((zlib.Z_SYNC_FLUSH, "deflate-sync-flush-per-row", "Z_SYNC_FLUSH after every scanline: empty stored blocks between, matches may reach across"),
			(zlib.Z_FULL_FLUSH, "deflate-full-flush-per-row", "Z_FULL_FLUSH after every scanline: no match reaches across a flush")):
		co = zlib.compressobj(6)
		parts = [co.compress(r[i:i + row]) + co.flush(flush) for i in range(0, len(r), row)]
		c.emit(name, zlib_png(b"".join(parts) + co.flush()), "display", "rfc1951-3.2.3#empty-block", what)
	w = D.BitWriter()
	for i in range(0, len(r), 7):
		D.stored_block(w, r[i:i + 7], False)
	D.stored_block(w, b"", True)
	c.emit("deflate-stored-small-blocks", wrap(w), "display", "rfc1951-3.2.3#multiple-blocks,rfc1951-3.2.3#empty-block",
		"7-byte stored blocks, then an empty final stored block")
	w = D.BitWriter()
	for _ in range(20):
		D.stored_block(w, b"", False)
	D.dynamic_block(w, [("lit", b) for b in r], True)
	c.emit("deflate-empty-blocks-first", wrap(w), "display", "rfc1951-3.2.3#empty-block", "20 empty stored blocks before the data")
	w = D.BitWriter()
	D.dynamic_block(w, [("lit", b) for b in r], True, dist_lengths={0: 1, 1: 1})
	c.emit("deflate-dynamic-literals", wrap(w), "display", "rfc1951-3.2.7#dynamic",
		"hand-written dynamic block, literals only, two unused distance codes (what zlib emits)")
	w = D.BitWriter()
	D.dynamic_block(w, [("lit", b) for b in r], True, dist_lengths={})
	c.emit("deflate-dynamic-no-distance-codes", wrap(w), "display", "rfc1951-3.2.7#no-distance-codes",
		"HDIST=1 with that code length 0: no distance codes at all, which RFC 1951 allows")
	up = raw(RGB8, 2)
	tokens = D.tokenize_runs(up)
	runs = sum(t[0] == "match" for t in tokens)
	assert runs, "the Up-filtered picture has no run of 3 repeated bytes"
	w = D.BitWriter()
	D.dynamic_block(w, tokens, True, dist_lengths={0: 1})
	c.emit("deflate-dynamic-one-distance-code", zlib_png(D.zlib_wrap(w.bytes(), up)), "display", "rfc1951-3.2.7#one-distance-code",
		f"a single distance code of length 1 (incomplete tree), which RFC 1951 allows; Up filter, so the repeated ground rows give {runs} distance-1 matches")
	# RFC 1951 3.2.7: HLIT is 257..286 and HDIST 1..32, so symbols 286/287 can never have a code in a dynamic block,
	# but distance codes 30 and 31 can (zlib refuses more than 30 unless built with PKZIP_BUG_WORKAROUND).
	# 32 codes of length 5, or 30 of length 5 and code 30 of length 4: both complete trees.
	for n, lengths, unused in ((32, {s: 5 for s in range(32)}, "distance codes 30 and 31 given lengths but never used; RFC-legal, refused by zlib without PKZIP_BUG_WORKAROUND"),
			(31, {s: 5 for s in range(30)} | {30: 4}, "distance code 30 given a length (4, so the tree is complete) but never used")):
		w = D.BitWriter()
		D.dynamic_block(w, [("lit", b) for b in r], True, dist_lengths=lengths)
		c.emit(f"deflate-dynamic-hdist-{n}-unused", wrap(w), "display", "rfc1951-3.2.7#hdist-over-30", f"HDIST={n}: {unused}")
	w = D.BitWriter()
	D.stored_block(w, r, True, nlen=0x1234)
	c.emit("deflate-stored-bad-nlen", wrap(w), "must-reject", "rfc1951-3.2.4#nlen", "stored block whose NLEN is not the complement of LEN", None)
	w = D.BitWriter()
	D.dynamic_block(w, [("lit", r[0]), ("match", 3, 5)] + [("lit", b) for b in r[4:]], True)
	c.emit("deflate-distance-too-far", wrap(w), "must-reject", "rfc1951-3.2.3#distance-too-far", "a match reaching 5 bytes back after 1 byte of output", None)
	lc = D.canonical_codes(D.FIXED_LIT)
	for name, clause, length_code, dist_code, what in (
			("deflate-fixed-length-code-286-used", "rfc1951-3.2.6#literal-286-287", 286, 0, "literal/length code 286, which has no meaning"),
			("deflate-fixed-distance-code-30-used", "rfc1951-3.2.5#distance-30-31", 257, 30, "distance code 30, which has no meaning")):
		w = D.BitWriter()
		w.bits(1, 1)
		w.bits(1, 2)
		for b in r[:10]:
			w.code(*lc[b])
		w.code(*lc[length_code])
		w.code(dist_code, 5)
		for b in r[10:]:
			w.code(*lc[b])
		w.code(*lc[256])
		c.emit(name, wrap(w), "must-reject", clause, f"fixed-Huffman block using {what}", None)
	# On the rgb8 picture every one of these encoders writes a single fixed-Huffman block; rgba16 is long enough
	# for dynamic blocks and real matches, which is what they are here to contribute.
	v16, r16 = TAGS["rgba16"], raw(TAGS["rgba16"])
	streams: dict[bytes, str] = {}
	for tool, label in (("zopfli", "zopfli"), ("libdeflate-gzip", "libdeflate"), ("7z", "7-zip")):
		z = external(tool, r16)
		if z is None:
			print(f"note: {tool} not installed; deflate-{label} not written", file=sys.stderr)
			continue
		assert zlib.decompress(z) == r16
		if z in streams:
			print(f"note: {tool} writes the same stream as {streams[z]}; deflate-{label} not written", file=sys.stderr)
			continue
		streams[z] = tool
		assert z[2] >> 1 & 3 == 2, f"{tool}: not a dynamic block"
		c.emit(f"deflate-{label}", zlib_png(z, v16), "display", "rfc1951-3.2.7#dynamic", f"the rgba16 picture written by {label}: dynamic blocks and real matches", v16)
	empty_dynamic_block(c)


def empty_dynamic_block(c: Corpus) -> None:
	r = raw(RGB8)
	w = D.BitWriter()
	D.stored_block(w, r, False)
	D.dynamic_block(w, [], True, lit_lengths={256: 1}, dist_lengths={})
	c.emit("deflate-empty-dynamic-block", zlib_png(D.zlib_wrap(w.bytes(), r)), "may-reject",
		"rfc1951-3.2.2#one-literal-code,rfc1951-3.2.3#empty-block,rfc1951-3.2.7#no-distance-codes",
		"the image data in a non-final stored block, then a final dynamic block that is empty: its literal/length code is the single one-bit code for 256, and it has no distance code")


# ---------------------------------------------------------------- 11.2.1 IHDR

def ihdr_raw(w: int, h: int, bd: int, ct: int, comp: int = 0, fm: int = 0, il: int = 0, length: int | None = None) -> bytes:
	body = struct.pack(">IIBBBBB", w, h, bd, ct, comp, fm, il)
	return B.chunk(b"IHDR", body if length is None else (body + bytes(8))[:length])


def quantized_rows(ct: int, bd: int) -> list[list[int]]:
	"""The picture at a colour type and depth Table 12 does not allow, quantized as picture.variant would."""
	pix = [[picture.picture(x, y) for x in range(picture.W)] for y in range(picture.H)]
	match ct:
		case 0:
			return [[picture.quantize(picture.luminance(*p[:3]), bd) for p in row] for row in pix]
		case 2:
			return [[picture.quantize(s, bd) for p in row for s in p[:3]] for row in pix]
		case 4:
			return [[picture.quantize(s, bd) for p in row for s in (picture.luminance(*p[:3]), p[3])] for row in pix]
		case 6:
			return [[picture.quantize(s, bd) for p in row for s in p] for row in pix]
	raise AssertionError(ct)


def ihdr(c: Corpus) -> None:
	W, H = picture.W, picture.H
	c.emit("ihdr-len-12", png(ihdr=ihdr_raw(W, H, 8, 2, length=12)), "must-reject", "11.2.1#length", "IHDR one byte short", None)
	c.emit("ihdr-len-14", png(ihdr=ihdr_raw(W, H, 8, 2, length=14)), "must-reject", "11.2.1#length", "IHDR one byte long", None)
	c.emit("ihdr-compression-1", png(ihdr=ihdr_raw(W, H, 8, 2, comp=1)), "must-reject", "11.2.1#compression-method",
		"compression method 1; data is ordinary zlib", None)
	c.emit("ihdr-filter-1", png(ihdr=ihdr_raw(W, H, 8, 2, fm=1)), "must-reject", "11.2.1#filter-method", "filter method 1; data uses filter method 0", None)
	c.emit("ihdr-interlace-2", png(ihdr=ihdr_raw(W, H, 8, 2, il=2)), "must-reject", "11.2.1#interlace-method", "interlace method 2; data is non-interlaced", None)
	c.emit("ihdr-width-0", png(ihdr=ihdr_raw(0, H, 8, 2)), "must-reject", "11.2.1#dimensions-nonzero", "width 0", None)
	c.emit("ihdr-height-0", png(ihdr=ihdr_raw(W, 0, 8, 2)), "must-reject", "11.2.1#dimensions-nonzero", "height 0", None)
	c.emit("ihdr-width-2-31", png(ihdr=ihdr_raw(0x80000000, H, 8, 2)), "must-reject", "11.2.1#dimensions-range",
		"width 2^31, beyond the PNG integer range", None)
	c.emit("ihdr-both-0", B.assemble([ihdr_raw(0, 0, 8, 2), B.chunk(b"IDAT", zlib.compress(b"")), IEND]), "must-reject", "11.2.1#dimensions-nonzero",
		"0x0 image with an empty zlib stream", None)
	for ct, bd in ((2, 4), (2, 1), (4, 4), (6, 2), (0, 3), (0, 0), (3, 16)):
		if ct == 3:
			rows, layout, plte = P8.rows, f"the p8 indices as {bd}-bit samples", [B.chunk(b"PLTE", plte_body(P8))]
			data = B.serialize(rows, W, H, 3, bd, filter_for=lambda p, y: 1)
		elif bd in (1, 2, 4, 8, 16):
			rows, layout, plte = quantized_rows(ct, bd), f"the picture quantized to {bd} bits", []
			data = B.serialize(rows, W, H, ct, bd, filter_for=lambda p, y: 1)
		else:
			v = VARIANTS[(ct, 8)]
			rows, layout, plte = v.rows, "the picture's 8-bit samples, laid out as 8-bit", []
			data = B.serialize(rows, W, H, ct, 8)
		parts = [ihdr_raw(W, H, bd, ct)] + plte + [B.chunk(b"IDAT", zlib.compress(data, 6)), IEND]
		c.emit(f"ihdr-{picture.CT_NAME[ct]}-bd{bd}", B.assemble(parts), "must-reject", "11.2.1#combination" if bd in (1, 2, 4, 8, 16) else "11.2.1#bit-depth",
			f"colour type {ct} at bit depth {bd} is not an allowed combination; {layout}", None)
	for ct in (1, 5, 7):
		c.emit(f"ihdr-ct{ct}", png(G8, ihdr=ihdr_raw(W, H, 8, ct)), "must-reject", "11.2.1#colour-type",
			f"colour type {ct} is undefined; data is 8-bit greyscale", None)
	z = zdata(RGB8)
	c.emit("ihdr-not-first", B.assemble([B.chunk(b"tEXt", b"k\0v"), B.ihdr(W, H, 8, 2), B.chunk(b"IDAT", z), IEND]), "must-reject", "11.2#ihdr-first",
		"a tEXt chunk precedes IHDR")
	c.emit("ihdr-dup", png(before=[B.ihdr(W, H, 8, 2)]), "must-reject", "11.2#one-ihdr", "two identical IHDR chunks")
	c.emit("ihdr-dup-different", png(before=[B.ihdr(2, 2, 8, 2)]), "must-reject", "11.2#one-ihdr", "a second IHDR giving 2x2")
	c.emit("ihdr-missing", B.assemble([B.chunk(b"IDAT", z), IEND]), "must-reject", "11.2#ihdr-first", "no IHDR at all", None)


# ---------------------------------------------------------------- 11.2.2 PLTE

def plte(c: Corpus) -> None:
	pb = plte_body(P8)
	pl = lambda body, **kw: B.chunk(b"PLTE", body, **kw)
	c.emit("plte-len-not-mult3-pal", png(P8, plte=[pl(pb + b"\x80")]), "must-reject", "11.2.2#invalid-indexed",
		f"palette length {len(pb) + 1} in an indexed image", None)
	c.emit("plte-len-not-mult3-rgb", png(before=[pl(pb + b"\x80")]), "recover", "11.2.2#invalid-truecolour",
		f"palette length {len(pb) + 1} in a truecolour image (suggested palette; 13.1 says an invalid PLTE in a truecolour image can be ignored)")
	c.emit("plte-empty-pal", png(P8, plte=[pl(b"")]), "must-reject", "11.2.2#invalid-indexed", "zero-length PLTE in an indexed image", None)
	c.emit("plte-empty-rgb", png(before=[pl(b"")]), "recover", "11.2.2#invalid-truecolour", "zero-length PLTE in a truecolour image")
	p1 = TAGS["p1"]
	extra = ((0, 0, 0), (255, 255, 255))
	assert not set(extra) & set(entries(p1))
	c.emit("plte-too-many-for-depth", image(p1, plte=plte_body(p1) + bytes(s for e in extra for s in e)), "may-reject", "11.2.2#entries-vs-depth",
		"1-bit indexed image with a 4-entry palette (p1's two entries, then black and white); only indices 0 and 1 are used", p1)
	c.emit("plte-257-entries", image(P8, plte=pb + bytes(3 * (257 - len(entries(P8))))), "must-reject", "11.2.2#invalid-indexed",
		f"771-byte PLTE: 257 entries (p8's {len(entries(P8))}, then black)", None)
	c.emit("plte-in-gray", png(G8, before=[pl(pb)]), "may-reject", "11.2.2#forbidden", "PLTE in a greyscale image, which shall not appear; pixels do not depend on it", G8)
	c.emit("plte-in-graya", png(GA8, before=[pl(pb)]), "may-reject", "11.2.2#forbidden", "PLTE in a greyscale+alpha image", GA8)
	c.emit("plte-after-idat-pal", png(P8, plte=[], after_idat=[pl(pb)]), "must-reject", "11.2.2#required", "indexed image whose PLTE follows IDAT", None)
	c.emit("plte-after-idat-rgb", png(after_idat=[pl(pb)]), "recover", "11.2.2#invalid-truecolour", "truecolour image with a suggested palette after IDAT")
	c.emit("plte-dup-rgb", png(before=[pl(pb), pl(pb[::-1])]), "may-reject", "11.2.2#single", "two suggested palettes in a truecolour image")
	c.emit("plte-crc-bad-rgb", png(before=[pl(pb, crc_value=1)]), "may-reject", "13.1#crc-critical", "bad CRC on an unused suggested palette")
	c.emit("plte-crc-bad-pal", png(P8, plte=[pl(pb, crc_value=1)]), "may-reject", "13.1#crc-critical", "bad CRC on the palette of an indexed image", P8)
	p2 = TAGS["p2"]
	cut = entries(p2)[:3]
	n3 = sum(s == (3,) for row in samples(p2) for s in row)
	assert n3, "p2 uses index 3"
	for name, trns, what in (("plte-index-beyond-2bit", None, ""), ("plte-index-beyond-2bit-trns", b"\x80\xff", "; tRNS 80 ff, and the out-of-range pixels stay opaque")):
		c.emit(name, image(p2, plte=bytes(s for e in cut for s in e), trns=trns), "recover", "13.1#index-opaque-black",
			f"p2 with its PLTE cut to three entries: the {n3} index-3 pixels must render opaque black (13.1){what}", pixels(p2, trns=trns, palette=cut))


# ---------------------------------------------------------------- 11.3.1.1 tRNS

def trns(c: Corpus) -> None:
	tr = lambda body, **kw: B.chunk(b"tRNS", body, **kw)
	p2 = TAGS["p2"]
	grey = Counter(s for row in samples(G8) for s in row).most_common(2)
	(g_a,), (g_b,) = grey[0][0], grey[1][0]
	c.emit("trns-len-1-gray", png(G8, before=[tr(bytes([g_a]))]), "recover", "11.3.1.1#length", "1-byte tRNS on greyscale (should be 2)", G8)
	c.emit("trns-len-3-gray", png(G8, before=[tr(bytes([0, g_a, 0]))]), "recover", "11.3.1.1#length", "3-byte tRNS on greyscale", G8)
	c.emit("trns-len-4-rgb", png(before=[tr(bytes(4))]), "recover", "11.3.1.1#length", "4-byte tRNS on truecolour (should be 6)")
	c.emit("trns-len-7-rgb", png(before=[tr(bytes(7))]), "recover", "11.3.1.1#length", "7-byte tRNS on truecolour")
	c.emit("trns-len-0-pal", png(P8, after_plte=[tr(b"")]), "display", "11.3.1.1#partial-palette", "zero-length tRNS on an indexed image: every entry stays opaque", P8)
	c.emit("trns-longer-than-plte", png(p2, after_plte=[tr(b"\x00\x40\x80\xc0\xff\xff")]), "recover", "11.3.1.1#length",
		"6 alpha values for a 4-entry palette: shall not", p2)
	c.emit("trns-in-graya", png(GA8, after_plte=[tr(b"\0\x80")]), "recover", "11.3.1.1#forbidden",
		"tRNS in a greyscale+alpha image: shall not appear; pixels have their own alpha", GA8)
	c.emit("trns-in-rgba", png(RGBA8, after_plte=[tr(b"\0\x80\0\x80\0\x80")]), "recover", "11.3.1.1#forbidden", "tRNS in a truecolour+alpha image", RGBA8)
	c.emit("trns-after-idat-gray", png(G8, after_idat=[tr(trns_for((g_a,)))]), "recover", "5.6#after-plte",
		f"tRNS after IDAT on greyscale (value {g_a} is the commonest grey of the picture)", G8)
	c.emit("trns-after-idat-pal", png(P8, after_idat=[tr(b"\0")]), "recover", "5.6#after-plte", "tRNS after IDAT on an indexed image", P8)
	c.emit("trns-before-plte-pal", png(P8, before=[tr(b"\0")]), "recover", "5.6#after-plte", "tRNS before PLTE on an indexed image (Chromium has an RGB case)", P8)
	c.emit("trns-dup-gray", png(G8, before=[tr(trns_for((g_a,))), tr(trns_for((g_b,)))]), "recover", "5.6#single-ancillary",
		f"two tRNS chunks naming the picture's two commonest greys, {g_a} and {g_b}; expected pixels: the first applied", pixels(G8, trns=trns_for((g_a,))))
	g4, g1, g2 = TAGS["g4"], TAGS["g1"], TAGS["g2"]
	(v4,) = mid(g4)
	for name, v, value, masked, what in (
			("trns-g4-highbits", g4, 0xF0 | v4, v4, f"4-bit grey, tRNS 0x{0xF0 | v4:04X}: decoders must mask to 0x{v4:X}"),
			("trns-g4-highbyte", g4, 0x100 | v4, v4, f"4-bit grey, tRNS 0x{0x100 | v4:04X}: masked to 0x{v4:X}"),
			("trns-g8-highbyte", G8, 0x100 | g_a, g_a, f"8-bit grey, tRNS 0x{0x100 | g_a:04X}: masked to 0x{g_a:02X}"),
			("trns-g1-value-2", g1, 2, 0, "1-bit grey, tRNS 0x0002: masked to 0, so black is transparent (not 'nonzero means white')"),
			("trns-g1-value-3", g1, 3, 1, "1-bit grey, tRNS 0x0003: masked to 1, white transparent"),
			("trns-g2-value-5", g2, 5, 1, "2-bit grey, tRNS 5: masked to 1")):
		assert occurs(v, (masked,)), (name, masked)
		c.emit(name, png(v, before=[tr(trns_for((value,)))]), "display", "11.3.1.1#mask", what, pixels(v, trns=trns_for((value,))))
	(r0, g0, b0), = samples(crop(RGB8, 1, 1))[0]
	body = trns_for((0x100 | r0, 0x100 | g0, 0x100 | b0))
	c.emit("trns-rgb8-highbyte", png(before=[tr(body)]), "display", "11.3.1.1#mask",
		"8-bit RGB, tRNS with a set high byte in each sample: masked, the top-left pixel's colour transparent", pixels(RGB8, trns=body))
	for name, v, what in (("trns-rgb8-1x1", crop(RGB8, 1, 1), "1x1 truecolour with a tRNS naming its only pixel (Pillow's blend_transparency.png is this shape)"),
			("trns-rgb8-2x1", crop(RGB8, 2, 1), "2x1 truecolour with a tRNS naming its pixels' colour: fewer pixels than tRNS samples"),
			("trns-rgb16-1x1", crop(RGB16, 1, 1), "1x1 16-bit truecolour with tRNS naming its pixel")):
		body = trns_for(samples(v)[0][0])
		want = pixels(v, trns=body)
		assert all(p[3] == 0 for row in want for p in row)
		c.emit(name, png(v, before=[tr(body)]), "display", "11.3.1.1#applied", what, want)
	for tag in ("g1", "g2", "g4", "g8", "g16", "rgb8", "rgb16"):
		v = TAGS[tag]
		top = (1 << v.bit_depth) - 1
		n = B.CHANNELS[v.colour_type]
		cases = [("black", (0,) * n), ("white", (top,) * n)] + ([("mid", mid(v))] if tag != "g1" else [])
		for which, value in cases:
			assert occurs(v, value), (tag, which, value)
			body = trns_for(value)
			c.emit(f"trns-{tag}-{which}", png(v, before=[tr(body)]), "display", "11.3.1.1#applied",
				f"tRNS names {which} ({', '.join(map(str, value))}), a colour of the picture: those pixels have alpha 0", pixels(v, trns=body))
	p1 = TAGS["p1"]
	for name, v, body, what in (("pal8-trns-entry0", P8, b"\0", "p8, tRNS of one byte 0: only entry 0 transparent"),
			("pal1-trns-entry1", p1, b"\xff\0", "p1, tRNS ff 00: entry 1 transparent"),
			("pal2-trns-entry3", p2, b"\xff\xff\xff\0", "p2, tRNS ff ff ff 00: entry 3 transparent")):
		c.emit(name, png(v, after_plte=[tr(body)]), "display", "11.3.1.1#applied", what, pixels(v, trns=body))


# ---------------------------------------------------------------- 11.2.4 IEND, 5 chunks, 9.2 filter type byte

def chunks(c: Corpus) -> None:
	z = zdata(RGB8)
	W, H = picture.W, picture.H
	c.emit("iend-missing", png(iend=None), "may-reject", "11.2#iend-present", "datastream ends after the last IDAT")
	c.emit("iend-len-4", png(iend=B.chunk(b"IEND", bytes(4))), "recover", "11.2#iend-last",
		"IEND with four data bytes (13.1: most decoders can ignore an invalid IEND)")
	c.emit("iend-dup", png(trailer=IEND), "recover", "11.2#iend-last", "two IEND chunks")
	c.emit("iend-crc-bad", png(iend=B.chunk(b"IEND", crc_value=0)), "may-reject", "13.1#crc-critical", "IEND with a wrong CRC")
	c.emit("iend-before-idat", B.assemble([B.ihdr(W, H, 8, 2), IEND, B.chunk(b"IDAT", z)]), "must-reject", "11.2#idat-present",
		"IEND before IDAT: no image data before the end", None)
	c.emit("trailing-garbage", png(trailer=b"GARBAGE" * 8), "recover", "11.2#iend-last", "56 bytes of non-chunk data after IEND")
	c.emit("trailing-partial-chunk", png(trailer=b"\0\0\0\x10tEXt"), "recover", "11.2#iend-last", "a truncated chunk header after IEND")

	# Private names (second letter lowercase) wherever the name is not the point, so each file has one fault.
	c.emit("chunk-unknown-critical-before-idat", png(before=[B.chunk(b"CrIT", bytes(8))]), "must-reject", "5.4#unknown-critical",
		"unknown critical chunk CrIT before IDAT: fatal for a decoder extracting the image")
	c.emit("chunk-unknown-critical-after-idat", png(after_idat=[B.chunk(b"CrIT", bytes(8))]), "must-reject", "5.4#unknown-critical",
		"unknown critical chunk CrIT after IDAT")
	c.emit("chunk-unknown-ancillary-public", png(before=[B.chunk(b"aBCD", bytes(8))]), "display", "5.7.2#undefined-public",
		"unknown ancillary public chunk: never an error for a decoder")
	c.emit("chunk-unknown-ancillary-private", png(before=[B.chunk(b"prIv", bytes(8))]), "display", "5.4#unknown-ancillary", "unknown ancillary private chunk")
	c.emit("chunk-unknown-ancillary-after-idat", png(after_idat=[B.chunk(b"prIv", bytes(8))]), "display", "5.4#unknown-ancillary",
		"unknown ancillary private chunk between IDAT and IEND")
	c.emit("chunk-reserved-bit-ancillary", png(before=[B.chunk(b"test", bytes(8))]), "display", "5.4#reserved-bit",
		"reserved bit set on an unknown ancillary private chunk (test): treat as unknown, do not flag")
	c.emit("chunk-reserved-bit-known", png(before=[B.chunk(b"gAma", GAMA)]), "display", "5.4#reserved-bit",
		"gAma: a known name with the reserved bit set is a different, unknown chunk")
	c.emit("chunk-reserved-bit-critical", png(before=[B.chunk(b"Test", bytes(8))]), "must-reject", "5.4#unknown-critical,5.4#reserved-bit",
		"reserved bit set on an unknown critical private chunk (Test): still an unknown critical chunk")
	for name, ctype, what in (("chunk-type-digit", b"t3Xt", "a digit: not a valid type; a decoder may treat as corruption"),
			("chunk-type-nul", b"teX\0", "a NUL byte"), ("chunk-type-space", b"teX ", "a space")):
		c.emit(name, png(before=[B.chunk(ctype, bytes(8))]), "may-reject", "5.3#type-letters", f"chunk type containing {what}")
	c.emit("chunk-len-2-31", png(before=[B.chunk(b"tEXt", b"k\0v", length=0x80000000)]), "must-reject", "5.3#length-limit",
		"ancillary chunk declaring length 2^31", None)
	c.emit("chunk-len-beyond-file", png(before=[B.chunk(b"tEXt", b"k\0v", length=100000)]), "must-reject", "11.2#idat-present",
		"ancillary chunk declaring more bytes than the file has", None)
	c.emit("chunk-ancillary-crc-bad", png(before=[B.chunk(b"tEXt", b"k\0v", crc_value=1)]), "may-reject", "13.1#crc-ancillary",
		"bad CRC on a tEXt chunk (13.1: a decoder may regard a CRC mismatch as fatal)")
	c.emit("chunk-unknown-ancillary-crc-bad", png(before=[B.chunk(b"prIv", bytes(8), crc_value=1)]), "may-reject", "13.1#crc-ancillary",
		"bad CRC on an unknown ancillary private chunk")
	c.emit("chunk-gama-crc-bad", png(before=[B.chunk(b"gAMA", GAMA, crc_value=1)]), "may-reject", "13.1#crc-ancillary", "bad CRC on gAMA")
	(g_a,), _ = Counter(s for row in samples(G8) for s in row).most_common(1)[0]
	c.emit("chunk-trns-crc-bad", png(G8, before=[B.chunk(b"tRNS", trns_for((g_a,)), crc_value=1)]), "may-reject", "13.1#crc-ancillary",
		f"bad CRC on tRNS naming grey {g_a}, a colour of the picture: dropping the chunk (recommended) leaves it opaque, using it makes it transparent, rejecting is also allowed", G8)
	c.emit("chunk-idat-crc-bad-first", png(idats=[B.chunk(b"IDAT", z[:8], crc_value=1), B.chunk(b"IDAT", z[8:])]), "may-reject", "13.1#crc-critical",
		"bad CRC on the first of two IDATs, data intact")
	c.emit("chunk-many-ancillary", png(before=[B.chunk(b"tEXt", f"k{i}\0v".encode()) for i in range(2000)]), "display", "11.3.3.2#text",
		"2000 tEXt chunks before IDAT")

	r = raw(RGB8)
	row = B.row_bytes(W, 2, 8) + 1
	for name, data, what in (("filter-type-5-first-row", b"\x05" + r[1:], "filter type 5 on the first scanline"),
			("filter-type-5-last-row", r[:-row] + b"\x05" + r[-row + 1:], "filter type 5 on the last scanline only"),
			("filter-type-255", b"\xff" + r[1:], "filter type 255 on the first scanline"),
			("filter-type-8", b"\x08" + r[1:], "filter type 8 (a decoder masking with 7 would read 0)")):
		assert len(data) == len(r)
		c.emit(name, zlib_png(zlib.compress(data, 6)), "must-reject", "9.2#filter-types", what, None)


# ---------------------------------------------------------------- ancillary chunks: content and position

def ancillary(c: Corpus) -> None:
	ch = B.chunk
	on = lambda *chunks, v=RGB8: png(v, before=list(chunks))
	after = lambda *chunks, v=RGB8: png(v, after_idat=list(chunks))
	def pal(*chunks: bytes, before: bool = False) -> bytes:
		return png(P8, before=list(chunks)) if before else png(P8, after_plte=list(chunks))
	t = lambda *f: struct.pack(">HBBBBB", *f)
	cases = [
		("gama-len-2", on(ch(b"gAMA", GAMA[:2])), "recover", "13.2#known-length", "gAMA with 2 bytes; recommended: ignore the chunk", RGB8),
		("gama-len-0", on(ch(b"gAMA", b"")), "recover", "13.2#known-length", "gAMA with 0 bytes", RGB8),
		("gama-len-5", on(ch(b"gAMA", GAMA + b"\0")), "recover", "13.2#known-length", "gAMA with 5 bytes", RGB8),
		("gama-value-0", on(ch(b"gAMA", bytes(4))), "recover", "11.3.2.2#zero", "gAMA 0 (a meaningless value)", RGB8),
		("gama-value-huge", on(ch(b"gAMA", b"\xff\xff\xff\xff")), "recover", "7.1#uint31", "gAMA 4294967295 (gamma 42949.67)", RGB8),
		("gama-after-plte", pal(ch(b"gAMA", GAMA)), "recover", "5.6#before-plte", "gAMA after PLTE: out of order; pixels unaffected", P8),
		("gama-after-idat", after(ch(b"gAMA", GAMA)), "recover", "5.6#before-plte", "gAMA after IDAT", RGB8),
		("gama-dup", on(ch(b"gAMA", GAMA), ch(b"gAMA", struct.pack(">I", 100000))), "recover", "5.6#single-ancillary",
			"two gAMA chunks (13.1 names this as a syntax error a decoder decides about)", RGB8),
		("srgb-len-2", on(ch(b"sRGB", b"\0\0")), "recover", "13.2#known-length", "sRGB with 2 bytes", RGB8),
		("srgb-intent-4", on(ch(b"sRGB", b"\x04")), "recover", "11.3.2.5#intent", "sRGB rendering intent 4 (defined: 0-3)", RGB8),
		("srgb-after-idat", after(ch(b"sRGB", b"\0")), "recover", "5.6#before-plte", "sRGB after IDAT", RGB8),
		("srgb-and-iccp", on(ch(b"sRGB", b"\0"), ch(b"iCCP", b"p\0\0" + zlib.compress(ICC_RGB))), "recover", "5.6#iccp-srgb",
			"sRGB and iCCP both present (should not)", RGB8),
		("srgb-gama-mismatch", on(ch(b"sRGB", b"\0"), ch(b"gAMA", struct.pack(">I", 100000))), "recover", "11.3.2.5#compat-values",
			"sRGB with gAMA 100000, not the 45455 sRGB implies", RGB8),
		("srgb-chrm-mismatch", on(ch(b"sRGB", b"\0"), ch(b"cHRM", SRGB_CHRM[:-4] + struct.pack(">I", 6001))), "recover", "11.3.2.5#compat-values",
			"sRGB with a cHRM whose last value is 6001, not the 6000 sRGB implies", RGB8),
		("chrm-len-30", on(ch(b"cHRM", bytes(30))), "recover", "13.2#known-length", "cHRM with 30 bytes", RGB8),
		("chrm-all-zero", on(ch(b"cHRM", bytes(32))), "display", "11.3.2.1#chrm", "cHRM with every value 0 (degenerate)", RGB8),
		("chrm-after-plte", pal(ch(b"cHRM", SRGB_CHRM)), "recover", "5.6#before-plte", "cHRM after PLTE", P8),
		("phys-len-8", on(ch(b"pHYs", bytes(8))), "recover", "13.2#known-length", "pHYs with 8 bytes", RGB8),
		("phys-unit-2", on(ch(b"pHYs", struct.pack(">IIB", 2835, 2835, 2))), "recover", "11.3.4.3#unit", "pHYs unit specifier 2 (defined: 0, 1)", RGB8),
		("phys-zero", on(ch(b"pHYs", struct.pack(">IIB", 0, 0, 1))), "display", "11.3.4.3#phys", "pHYs 0 pixels per metre", RGB8),
		("phys-after-idat", after(ch(b"pHYs", struct.pack(">IIB", 2835, 2835, 1))), "recover", "5.6#before-idat", "pHYs after IDAT", RGB8),
		("time-len-6", on(ch(b"tIME", b"\x07\xe9\x01\x01\x00\x00")), "recover", "13.2#known-length", "tIME with 6 bytes", RGB8),
		("time-month-13", on(ch(b"tIME", t(2025, 13, 1, 0, 0, 0))), "recover", "11.3.5.1#range", "tIME month 13", RGB8),
		("time-day-0", on(ch(b"tIME", t(2025, 1, 0, 0, 0, 0))), "recover", "11.3.5.1#range", "tIME day 0", RGB8),
		("time-second-60", on(ch(b"tIME", t(2025, 1, 1, 0, 0, 60))), "display", "11.3.5.1#time", "tIME second 60 (allowed for leap seconds)", RGB8),
		("time-year-0", on(ch(b"tIME", t(0, 1, 1, 0, 0, 0))), "display", "11.3.5.1#time", "tIME year 0", RGB8),
		("time-dup", on(ch(b"tIME", t(2025, 1, 1, 0, 0, 0)), ch(b"tIME", t(2024, 1, 1, 0, 0, 0))), "recover", "5.6#single-ancillary", "two tIME chunks", RGB8),
		("text-no-null", on(ch(b"tEXt", b"Comment")), "recover", "11.3.3.2#separator", "tEXt without a null separator", RGB8),
		("text-empty-keyword", on(ch(b"tEXt", b"\0text")), "recover", "11.3.3.1#keyword-length", "tEXt with an empty keyword", RGB8),
		("text-keyword-80", on(ch(b"tEXt", b"k" * 80 + b"\0v")), "recover", "11.3.3.1#keyword-length", "tEXt keyword of 80 characters (limit 79)", RGB8),
		("text-len-0", on(ch(b"tEXt", b"")), "recover", "11.3.3.2#separator", "zero-length tEXt", RGB8),
		("text-keyword-spaces", on(ch(b"tEXt", b" Comment \0v")), "recover", "11.3.3.1#keyword-spaces", "tEXt keyword with leading and trailing spaces", RGB8),
		("text-control-char", on(ch(b"tEXt", b"Comment\0a\x1b[31mb")), "recover", "11.3.3.2#latin1", "tEXt whose text holds ESC, a C0 control", RGB8),
		("text-second-null", on(ch(b"tEXt", b"Comment\0a\x00c")), "recover", "11.3.3.2#null", "tEXt whose text holds a second NUL", RGB8),
		("text-non-latin1", on(ch(b"tEXt", b"Comment\0\xf0\x9f\x98\x80\xff\xfe")), "recover", "11.3.3.2#latin1", "tEXt with bytes that are not Latin-1 text", RGB8),
		("ztxt-method-1", on(ch(b"zTXt", b"Comment\0\x01" + zlib.compress(b"hi"))), "recover", "11.3.3.3#method", "zTXt compression method 1", RGB8),
		("ztxt-bad-zlib", on(ch(b"zTXt", b"Comment\0\0\xff\xff\xff\xff")), "recover", "11.3.3.3#zlib", "zTXt whose zlib stream is garbage", RGB8),
		("ztxt-bad-adler", on(ch(b"zTXt", b"Comment\0\0" + zlib.compress(b"hello")[:-4] + bytes(4))), "recover", "11.3.3.3#zlib",
			"zTXt with a wrong Adler-32 (the image-rs iCCP case, on text)", RGB8),
		("ztxt-no-null", on(ch(b"zTXt", b"Comment")), "recover", "11.3.3.3#separator", "zTXt without a null separator", RGB8),
		("ztxt-len-1", on(ch(b"zTXt", b"\0")), "recover", "11.3.3.1#keyword-length", "zTXt of one byte", RGB8),
		("ztxt-bomb", on(ch(b"zTXt", b"Comment\0\0" + zlib.compress(bytes(64 << 20), 9))), "recover", "11.3.3.3#null",
			"zTXt that inflates to 64 MiB of zeros (so its text holds NULs)", RGB8),
		("itxt-flag-2", on(ch(b"iTXt", b"Comment\0\x02\0\0\0text")), "recover", "11.3.3.4#flag", "iTXt compression flag 2", RGB8),
		("itxt-method-1", on(ch(b"iTXt", b"Comment\0\x01\x01\0\0" + zlib.compress(b"text"))), "recover", "11.3.3.4#method", "iTXt compressed with method 1", RGB8),
		("itxt-bad-zlib", on(ch(b"iTXt", b"Comment\0\x01\0\0\0\xff\xff\xff\xff")), "recover", "11.3.3.4#zlib", "iTXt compressed flag set, garbage stream", RGB8),
		("itxt-truncated", on(ch(b"iTXt", b"Comment\0")), "recover", "11.3.3.4#separators", "iTXt ending after the keyword", RGB8),
		("itxt-bad-utf8", on(ch(b"iTXt", b"Comment\0\0\0en\0\xff\xfe\0\xc0\xc0")), "recover", "11.3.3.4#utf8", "iTXt whose text is not UTF-8", RGB8),
		("itxt-language-numeric", on(ch(b"iTXt", b"Comment\0\0\0" + b"123" + b"\0\0text")), "recover", "11.3.3.4#language",
			"iTXt with language tag 123, which is not a language tag", RGB8),
		("sbit-len-wrong", on(ch(b"sBIT", b"\x04\x04\x04"), v=G8), "recover", "13.2#known-length", "sBIT with 3 bytes on greyscale (should be 1)", G8),
		("sbit-value-0", on(ch(b"sBIT", bytes(3))), "recover", "11.3.2.4#range", "sBIT 0", RGB8),
		("sbit-value-9", on(ch(b"sBIT", b"\x09\x09\x09")), "recover", "11.3.2.4#range", "sBIT 9 on an 8-bit image", RGB8),
		("sbit-after-plte", pal(ch(b"sBIT", b"\x08\x08\x08")), "recover", "5.6#before-plte", "sBIT after PLTE", P8),
		("sbit-rgb16-17", on(ch(b"sBIT", b"\x11\x11\x11"), v=RGB16), "recover", "11.3.2.4#range", "sBIT 17 on a 16-bit image", RGB16),
		("bkgd-len-wrong-gray", on(ch(b"bKGD", b"\0"), v=G8), "recover", "13.2#known-length", "bKGD of 1 byte on greyscale (should be 2)", G8),
		("bkgd-len-wrong-rgb", on(ch(b"bKGD", b"\0\0")), "recover", "13.2#known-length", "bKGD of 2 bytes on truecolour (should be 6)", RGB8),
		("bkgd-pal-index-beyond", pal(ch(b"bKGD", bytes([len(entries(P8))]))), "recover", "11.3.4.1#index",
			f"bKGD index {len(entries(P8))} for a {len(entries(P8))}-entry palette", P8),
		("bkgd-before-plte", pal(ch(b"bKGD", b"\x01"), before=True), "recover", "5.6#after-plte", "bKGD before PLTE", P8),
		("bkgd-after-idat", after(ch(b"bKGD", b"\0\x80"), v=G8), "recover", "5.6#after-plte", "bKGD after IDAT", G8),
		("bkgd-gray-highbyte", on(ch(b"bKGD", b"\x01\x80"), v=G8), "display", "11.3.4.1#mask", "bKGD 0x0180 on 8-bit greyscale", G8),
		("bkgd-len-4", on(ch(b"bKGD", b"\0\x80\0\x80"), v=G8), "recover", "13.2#known-length", "bKGD of 4 bytes on greyscale (no colour type uses 4)", G8),
		("hist-len-wrong", pal(ch(b"hIST", b"\0\x01\0\x02")), "recover", "11.3.4.2#entries", f"hIST with 2 entries for a {len(entries(P8))}-entry palette", P8),
		("hist-before-plte", pal(ch(b"hIST", b"\0\x01" * len(entries(P8))), before=True), "recover", "5.6#after-plte", "hIST before PLTE", P8),
		("hist-without-plte", on(ch(b"hIST", b"\0\x01" * 4)), "recover", "11.3.4.2#entries", "hIST in an image with no palette", RGB8),
		("splt-depth-7", on(ch(b"sPLT", b"name\0\x07" + bytes(6))), "recover", "11.3.4.4#depth", "sPLT sample depth 7 (defined: 8, 16)", RGB8),
		("splt-len-wrong", on(ch(b"sPLT", b"name\0\x08" + bytes(7))), "recover", "11.3.4.4#length",
			"sPLT whose entries are not a whole number of 6-byte records", RGB8),
		("splt-no-null", on(ch(b"sPLT", b"name")), "recover", "11.3.4.4#separator", "sPLT without a null separator", RGB8),
		("splt-dup-name", on(ch(b"sPLT", b"name\0\x08" + bytes(6)), ch(b"sPLT", b"name\0\x08" + b"\xff" * 6)), "recover", "11.3.4.4#unique-name",
			"two sPLT chunks with the same name (shall not)", RGB8),
		("splt-after-idat", after(ch(b"sPLT", b"name\0\x08" + bytes(6))), "recover", "5.6#before-idat", "sPLT after IDAT", RGB8),
	]
	prof = zlib.compress(ICC_RGB)
	cases += [
		("iccp-method-1", on(ch(b"iCCP", b"p\0\x01" + prof)), "recover", "11.3.2.3#method", "iCCP compression method 1", RGB8),
		("iccp-name-empty", on(ch(b"iCCP", b"\0\0" + prof)), "recover", "11.3.3.1#keyword-length", "iCCP with an empty profile name", RGB8),
		("iccp-name-80", on(ch(b"iCCP", b"p" * 80 + b"\0\0" + prof)), "recover", "11.3.3.1#keyword-length", "iCCP profile name of 80 characters", RGB8),
		("iccp-no-null", on(ch(b"iCCP", b"profile")), "recover", "11.3.2.3#separator", "iCCP without a null separator", RGB8),
		("iccp-profile-empty", on(ch(b"iCCP", b"p\0\0" + zlib.compress(b""))), "recover", "11.3.2.3#profile", "iCCP whose profile decompresses to 0 bytes", RGB8),
		("iccp-profile-garbage", on(ch(b"iCCP", b"p\0\0" + zlib.compress(b"not an ICC profile" * 10))), "recover", "11.3.2.3#profile",
			"iCCP whose profile is not an ICC profile", RGB8),
		("iccp-bad-zlib-header", on(ch(b"iCCP", b"p\0\0\xff\xff" + prof[2:])), "recover", "11.3.2.3#zlib", "iCCP whose zlib header is invalid", RGB8),
		("iccp-truncated-zlib", on(ch(b"iCCP", b"p\0\0" + prof[:len(prof) // 2])), "recover", "11.3.2.3#zlib", "iCCP whose zlib stream stops halfway", RGB8),
		("iccp-after-plte", pal(ch(b"iCCP", b"p\0\0" + prof)), "recover", "5.6#before-plte", "iCCP after PLTE", P8),
		("iccp-dup", on(ch(b"iCCP", b"p\0\0" + prof), ch(b"iCCP", b"q\0\0" + prof)), "recover", "5.6#single-ancillary", "two iCCP chunks", RGB8),
		("iccp-bomb", on(ch(b"iCCP", b"p\0\0" + zlib.compress(bytes(64 << 20), 9))), "recover", "11.3.2.3#profile", "iCCP that inflates to 64 MiB", RGB8),
		("exif-len-0", on(ch(b"eXIf", b"")), "recover", "11.3.4.5#header", "zero-length eXIf", RGB8),
		("exif-garbage", on(ch(b"eXIf", b"Lorem ipsum dolor sit amet")), "recover", "11.3.4.5#header",
			"eXIf whose payload is not a TIFF header (wuffs's exif.png is the same idea at 1x1)", RGB8),
		("exif-tiff-header-only", on(ch(b"eXIf", b"II*\0\x08\0\0\0")), "recover", "11.3.4.5#structure", "eXIf with a TIFF header and no IFD", RGB8),
		("exif-bad-ifd-offset", on(ch(b"eXIf", b"MM\0*\xff\xff\xff\xf0")), "recover", "11.3.4.5#structure",
			"eXIf whose first IFD offset points far outside the chunk", RGB8),
		("exif-after-idat", after(ch(b"eXIf", b"II*\0\x08\0\0\0\0\0")), "recover", "5.6#before-idat", "eXIf after IDAT (table 7: before IDAT)", RGB8),
		("cicp-len-3", on(ch(b"cICP", b"\x01\x0d\x00")), "recover", "13.2#known-length", "cICP with 3 bytes", RGB8),
		("cicp-primaries-200", on(ch(b"cICP", b"\xc8\x0d\x00\x01")), "recover", "11.3.2.6#primaries", "cICP colour primaries 200, a code point H.273 reserves", RGB8),
		("cicp-transfer-200", on(ch(b"cICP", b"\x01\xc8\x00\x01")), "recover", "11.3.2.6#transfer", "cICP transfer characteristics 200, a code point H.273 reserves", RGB8),
		("cicp-matrix-1", on(ch(b"cICP", b"\x01\x0d\x01\x01")), "recover", "11.3.2.6#matrix", "cICP matrix coefficients 1 (PNG requires 0)", RGB8),
		("cicp-range-2", on(ch(b"cICP", b"\x01\x0d\x00\x02")), "recover", "11.3.2.6#full-range", "cICP video full range flag 2", RGB8),
		("cicp-after-plte", pal(ch(b"cICP", b"\x01\x0d\x00\x01")), "recover", "5.6#before-plte", "cICP after PLTE", P8),
		("mdcv-len-wrong", on(ch(b"cICP", b"\x09\x10\x00\x01"), ch(b"mDCV", bytes(20))), "recover", "13.2#known-length",
			"mDCV with 20 bytes (should be 24), with the cICP it must accompany", RGB8),
		("clli-len-wrong", on(ch(b"cLLI", bytes(6))), "recover", "13.2#known-length", "cLLI with 6 bytes (should be 8)", RGB8),
		("actl-len-7", on(ch(b"acTL", b"\0\0\0\x01\0\0\0")), "recover", "11.3.6.1#length",
			"acTL with 7 bytes and no frame control chunks: a still image with a broken animation header", RGB8),
		("actl-frames-0", on(ch(b"acTL", bytes(8))), "recover", "11.3.6.1#num-frames-zero",
			"acTL declaring 0 frames and no fcTL: the default image is all there is", RGB8),
		("actl-after-idat", after(ch(b"acTL", b"\0\0\0\x01\0\0\0\0")), "recover", "11.3.6.1#before-idat", "acTL after IDAT, no fcTL", RGB8),
	]
	W, H = picture.W, picture.H
	actl1 = ch(b"acTL", b"\0\0\0\x01\0\0\0\0")
	fctl = lambda w, h, dispose: ch(b"fcTL", struct.pack(">IIIIIHHBB", 0, w, h, 0, 0, 1, 10, dispose, 0))
	cases += [
		("fctl-len-25", on(actl1, ch(b"fcTL", bytes(25))), "recover", "11.3.6.2#length", "fcTL of 25 bytes before IDAT", RGB8),
		("fctl-dims-mismatch", on(actl1, fctl(2, 2, 0)), "recover", "11.3.6.2#default-image",
			f"fcTL for the IDAT frame declares 2x2 for a {W}x{H} image: an APNG error, the static image is still complete", RGB8),
		("fctl-dispose-3", on(actl1, fctl(W, H, 3)), "recover", "11.3.6.2#dispose-op",
			"fcTL for the IDAT frame with dispose_op 3 (undefined): an APNG error, the static image is still complete", RGB8),
		("fdat-without-fctl", after(ch(b"acTL", b"\0\0\0\x02\0\0\0\0"), ch(b"fdAT", b"\0\0\0\x01" + zdata(RGB8))), "recover", "11.3.6.1#before-idat",
			"acTL after IDAT, fdAT with no fcTL: broken animation, complete still image", RGB8),
	]
	for name, data, expect3, clause, note, v in cases:
		c.emit(name, data, expect3, clause, note, v)


# ---------------------------------------------------------------- iDOT (Apple)

def idot_segments(heights: list[int], gap: int = 0) -> tuple[bytes, list[bytes]]:
	"""(iDOT body, IDAT chunks) for rgba8 in segments of the given row counts; `gap` bytes of chunks lie between
	iDOT and the first IDAT. Each segment's data is its own IDAT and starts at a byte-aligned block boundary."""
	v = RGBA8
	assert sum(heights) == v.height and all(h > 0 for h in heights)
	r = raw(v)
	row = B.row_bytes(v.width, 6, 8) + 1
	assert all(r[y * row] == 0 for y in range(v.height)), "every row filter None"
	co = zlib.compressobj(6)
	datas, first = [], 0
	for i, h in enumerate(heights):
		last = i == len(heights) - 1
		datas.append(co.compress(r[first * row:(first + h) * row]) + co.flush(zlib.Z_FINISH if last else zlib.Z_FULL_FLUSH))
		assert last or datas[-1].endswith(b"\x00\x00\xff\xff"), "a full flush ends in an empty stored block, byte-aligned"
		first += h
	assert zlib.decompress(b"".join(datas)) == r
	idats = [B.chunk(b"IDAT", d) for d in datas]
	offset, first, entries = 12 + 4 + 12 * len(heights) + gap, 0, []  # offsets count from iDOT's length field
	for h, idat in zip(heights, idats):
		entries += [first, h, offset]
		first, offset = first + h, offset + len(idat)
	return struct.pack(f">{1 + len(entries)}I", len(heights), *entries), idats


def idot_offsets_land(data: bytes) -> list[bool]:
	"""For each iDOT segment: whether its offset lands on an IDAT chunk."""
	at = data.index(b"iDOT") - 4
	body = data[at + 8:at + 8 + struct.unpack(">I", data[at:at + 4])[0]]
	n = struct.unpack(">I", body[:4])[0]
	return [data[at + off + 4:at + off + 8] == b"IDAT" for _, _, off in (struct.unpack(">3I", body[4 + 12 * i:16 + 12 * i]) for i in range(n))]


def idot(c: Corpus) -> None:
	v = RGBA8
	ihdr_ = B.ihdr(v.width, v.height, 8, 6)
	srgb = B.chunk(b"sRGB", b"\0")
	def file(body: bytes | None, idats: list[bytes], *, before=(), between=()) -> bytes:
		return B.assemble([ihdr_, *before] + ([B.chunk(b"iDOT", body)] if body is not None else []) + [*between, *idats, IEND])
	files = []
	for name, heights in (("chunk-idot", [8, 7]), ("chunk-idot-1", [15]), ("chunk-idot-3", [5, 5, 5])):
		files.append((name, file(*idot_segments(heights)), "", f"iDOT with segments of {', '.join(map(str, heights))} rows, each in its own IDAT after a full flush"))
	files.append(("chunk-idot-after-srgb", file(*idot_segments([8, 7]), before=[srgb]), "", "sRGB, then iDOT: offsets count from iDOT, so they are unchanged"))
	files.append(("chunk-idot-before-srgb", file(*idot_segments([8, 7], len(srgb)), between=[srgb]), "",
		f"iDOT, then sRGB: the first offset is {12 + 28 + len(srgb)}"))
	for name, data, _, _ in files:
		assert all(idot_offsets_land(data)), name
	body, idats = idot_segments([8, 7])
	stale = file(body, [B.chunk(b"IDAT", b"".join(i[8:-4] for i in idats))])
	assert idot_offsets_land(stale) == [True, False], "the stale iDOT's second offset misses every IDAT chunk"
	files.append(("chunk-idot-stale", stale, "idot#offsets", "an optimizer merged the IDAT chunks and passed iDOT through unchanged: the second offset lands inside the merged IDAT's data"))
	assert len(body) == 28
	files.append(("chunk-idot-short", file(body[:24], idats), "idot#layout", "iDOT of six of its seven words: no second segment offset"))
	files.append(("chunk-idot-empty", file(b"", idats), "idot#layout", "zero-length iDOT"))
	for name, data, clause, note in files:
		r = c.emit(name, data, "display", clause, note, v)
		assert "idot#public-name" in r.verdicts["3"].strict_notes, name


SECTIONS = (filters, adam7, packing, structure, deflate, ihdr, plte, trns, chunks, ancillary, idot)


def main() -> None:
	out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "synth"
	assert len(sys.argv) <= 2, "usage: synth.py [DIR]"
	out.mkdir(parents=True, exist_ok=True)
	for p in out.glob("*.png"):
		p.unlink()
	with tempfile.TemporaryDirectory() as scratch:
		c = Corpus(out, Path(scratch))
		for section in SECTIONS:
			section(c)
	with open(out / "index.tsv", "w") as f:
		f.write("name\texpect3\tclause\tnote\n")
		for row in c.index:
			f.write("\t".join(row) + "\n")
	print(f"{len(c.index)} files")


if __name__ == "__main__":
	main()
