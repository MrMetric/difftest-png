"""Plain-assert regressions and read-only corpus comparison.

	uv run python test_refpng.py
	uv run python test_refpng.py --manifest --report /tmp/refpng-comparison.json

Both commands enter a systemd scope with MemoryMax=16G and MemorySwapMax=0 before
running any decodes. --manifest decodes EVERY decode/**/*.png once and compares the manifest's
derived columns with what expect.py would write for it: every interpretation's choices and digest
(pam.digest's exact canonical representation), the reached clauses, and each lens's conformance and
expectation; it never runs run.py or the pool. --baseline DIR compares diagnostics and every
interpretation's pixels to a saved refpng.py + inflate.py (and pngfields.py when present) with the
same decode API. A saved pngdiagnostics.py and spec/clauses.tsv isolate the baseline's policies.
With a baseline, stale manifest differences are informational; any baseline pixel difference still
fails the run.
"""
import argparse
import ast
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import zlib

import inflate
import pnglens
import refpng
from pngdiagnostics import CONSEQUENCES, Diagnostics, StreamDiagnostics
from pngfields import (Parser, Values, Ignored, Layout, Struct, UInt, Repeat, Choice,
	Text, Content, Fragment, UINT31, UINT16, BYTE, LANGUAGE_TAG)

ROOT = Path(__file__).resolve().parent


def chunk(name, body):
	return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body))


def png(metadata=(), *, raw=b"\0\x12\x34\x56", stream=None, width=1, height=1, depth=8, color=2,
		interlace=0, after=()):
	ihdr = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)
	return (refpng.SIGNATURE + chunk(b"IHDR", ihdr) + b"".join(chunk(*c) for c in metadata)
		+ chunk(b"IDAT", zlib.compress(raw) if stream is None else stream)
		+ b"".join(chunk(*c) for c in after) + chunk(b"IEND", b""))


def check(data, *, warning=None, error=None, no_image=None, pixels=None, clean=False, choices=((),)):
	"""Decode and return (the first interpretation's image or None, the record).

	`choices` lists every interpretation's choices in order; by default a file has no open decision.
	`pixels` are the first interpretation's samples. `warning` and `error` name a WARN and an ERR fault."""
	record = Diagnostics()
	try:
		decoded = refpng.decode(data, diagnostics=record)
	except refpng.NoImage as e:
		assert e.id == no_image, (e.id, no_image, str(e))
		assert e.diagnostics is record
		image = None
	else:
		assert no_image is None, f"decoded, expected NoImage {no_image}"
		assert decoded.diagnostics is record
		got = tuple(i.choices for i in decoded.interpretations)
		assert got == tuple(choices), (got, choices)
		image = decoded.interpretations[0].image
		if pixels is not None:
			assert tuple(image.samples) == tuple(pixels), (tuple(image.samples), pixels)
	if warning is not None:
		assert warning in {id for id, _ in record.warnings}, (warning, record.warnings)
	if error is not None:
		assert any(e.id == error and e.level == "ERR" for e in record.events), (error, record.events)
	if clean:
		assert not faults(record) and image is not None, faults(record)
	assert_reported(record)
	return image, record


def interpretations(data):
	return {i.choices: tuple(i.image.samples) for i in refpng.decode(data).interpretations}


def faults(record):
	return [e for e in record.events if e.level != "INFO"]


def stdout(record):
	out = io.StringIO()
	with redirect_stdout(out):
		record.emit()
	return out.getvalue()


def assert_reported(record):
	"""Every fault clause the record reaches is reported by a WARN or ERR event, as its id or its cause."""
	reported = {id for e in record.events if e.level != "INFO" for id in (e.id, e.cause) if id is not None}
	unreported = {id for id in record.reached if CONSEQUENCES[id] != "none"} - reported
	assert not unreported, unreported


def expectations(record, no_image=False):
	"""Each lens's expectation for the recorded datastream."""
	return {name: pnglens.verdict(record, lens, no_image).expectation for name, lens in pnglens.LENSES.items()}


def both(expectation):
	return {name: expectation for name in pnglens.LENSES}


def test_diagnostic_invariant():
	for id, consequence in CONSEQUENCES.items():
		d = Diagnostics()
		if consequence == "none":
			d.record(id)
			assert d.reached == {id}
		else:
			try:
				d.record(id)
			except AssertionError as e:
				assert "requires a diagnostic" in str(e)
			else:
				raise AssertionError(f"coverage-only fault accepted: {id}")
			assert not d.events, "failed insertion must be atomic"
			d.record(id, "ERR" if consequence == "reject" else "WARN", "deliberate fault")
			assert_reported(d)
	d = Diagnostics()
	d.record("7.1#uint31", "WARN", "range")
	warnings = d.warnings
	warnings.clear()
	assert d.warnings, "callers cannot mutate the record through a projection"
	try:
		d.reached.add("5.2#signature")
	except AttributeError:
		pass
	else:
		raise AssertionError("coverage projection is writable")
	try:
		inflate.zlib_decompress(zlib.compress(b""), set())
	except AssertionError as e:
		assert "shared diagnostic sink" in str(e)
	else:
		raise AssertionError("inflate accepted a coverage-only sink")
	# Every literal coverage call in the implementation agrees with the catalog.
	for path in ("refpng.py", "inflate.py", "pngfields.py", "pngdiagnostics.py"):
		for node in ast.walk(ast.parse((ROOT / path).read_text())):
			if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reach":
				if node.args and isinstance(node.args[0], ast.Constant):
					assert CONSEQUENCES[node.args[0].value] == "none", (path, node.lineno)


def test_layout_invariant():
	# A new chunk gets the uint31 rule just by declaring its layout. No offset table or handler check exists.
	calls = []
	def handler(d, f):
		assert isinstance(f, Values) and f.counter == 9
		try:
			f[0:4]
		except TypeError:
			pass
		else:
			raise AssertionError("handler can slice raw fields")
		calls.append(f)
	name = b"teST"
	assert name not in refpng.CHUNKS
	refpng.CHUNKS[name] = refpng.Chunk(handler, refpng.layout(("counter", UINT31)))
	try:
		check(png(((name, struct.pack(">I", 9)),)), clean=True)
		assert len(calls) == 1
		check(png(((name, struct.pack(">I", 0x80000000)),)), warning="7.1#uint31")
		assert len(calls) == 1, "handler observed an invalid typed value"
	finally:
		del refpng.CHUNKS[name]
	# Nested and repeated fields use exactly the same rule.
	d = refpng._State(Diagnostics())
	schema = Layout(Struct((("entries", Repeat(Struct((("short", UINT16), ("long", UInt(4)))), 2)),)))
	try:
		Parser(d, b"teST", struct.pack(">HIHI", 65535, 7, 1, 0xffffffff), schema).parse()
	except Ignored:
		assert d.diagnostics.warnings[0][0] == "7.1#uint31"
	else:
		raise AssertionError("nested uint31 rule skipped")
	# A metadata registration cannot use the image-data escape hatch.
	try:
		Parser(d, b"teST", b"opaque", Layout(Struct((("blob", Fragment()),)))).parse()
	except AssertionError as e:
		assert "metadata cannot opt out" in str(e)
	else:
		raise AssertionError("unvalidated metadata accepted")
	# Each registered handler has only state and parsed values as arguments.
	for definition in refpng.CHUNKS.values():
		assert definition.handler.__annotations__["f"] is Values


SINGLETON_PNG = bytes.fromhex(
	"89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55"
	"00000019494441547801000200fdff007f05c0010500000000a0ffaf0300810080"
	"76000e0b0000000049454e44ae426082")
SINGLETON_STREAM = bytes.fromhex("7801000200fdff007f05c0010500000000a0ffaf0300810080")


def test_audit_singleton():
	assert zlib.decompress(SINGLETON_STREAM) == b"\0\x7f"
	check(SINGLETON_PNG, pixels=(127, 127, 127, 255), warning="rfc1951-3.2.2#one-literal-code")
	check(png(stream=SINGLETON_STREAM, color=0), pixels=(127, 127, 127, 255))


def test_audit_uint31():
	cases = ((b"gAMA", struct.pack(">I", 0x80000000)),
		(b"cHRM", struct.pack(">8I", *([0x80000000] * 8))),
		(b"pHYs", struct.pack(">IIB", 0x80000000, 1, 1)))
	for case in cases:
		check(png((case,)), warning="7.1#uint31", pixels=(18, 52, 86, 255))
	# Derive mutation positions from the layouts themselves, including every edition-3 uint31 field.
	def offsets(field, base=0):
		if isinstance(field, UInt):
			return ([base] if field.width == 4 else []), field.width
		if isinstance(field, Struct):
			out, size = [], 0
			for _, item in field.fields:
				found, n = offsets(item, base + size)
				out.extend(found)
				size += n
			return out, size
		if isinstance(field, Repeat) and isinstance(field.count, int):
			out, size = [], 0
			for _ in range(field.count):
				found, n = offsets(field.item, base + size)
				out.extend(found)
				size += n
			return out, size
		if isinstance(field, Fragment):
			return [], 0
		raise AssertionError(field)
	for name in (b"IHDR", b"cHRM", b"gAMA", b"pHYs", b"mDCV", b"cLLI", b"acTL", b"fcTL", b"fdAT"):
		schema = refpng.CHUNKS[name].layout
		positions, size = offsets(schema.fields)
		assert positions
		valid = bytearray(size)
		if name == b"IHDR":
			valid[:] = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
		for pos in positions:
			body = valid.copy()
			body[pos:pos + 4] = b"\x80\0\0\0"
			d = refpng._State(Diagnostics())
			try:
				Parser(d, name, bytes(body), schema, fatal=name == b"IHDR").parse()
			except (Ignored, refpng.NoImage):
				assert "11.2.1#dimensions-range" in d.diagnostics.reached or "7.1#uint31" in d.diagnostics.reached
			else:
				raise AssertionError((name, pos, "uint31 escaped validation"))
			assert_reported(d.diagnostics)


def test_audit_srgb():
	bad_chrm = (*refpng.SRGB_CHRM[:-1], 6001)
	for metadata in ((b"gAMA", struct.pack(">I", 100000)), (b"cHRM", struct.pack(">8I", *bad_chrm))):
		for chunks in ((metadata, (b"sRGB", b"\0")), ((b"sRGB", b"\0"), metadata)):
			check(png(chunks), warning="11.3.2.5#compat-values")
	check(png(((b"gAMA", struct.pack(">I", 45455)), (b"cHRM", struct.pack(">8I", *refpng.SRGB_CHRM)),
		(b"sRGB", b"\0"))), clean=True)


def itxt(language, text=b"text", *, compressed=False, translated=b"", keyword=b"Comment", method=0):
	return keyword + b"\0" + bytes((int(compressed), method)) + language + b"\0" + translated + b"\0" + (zlib.compress(text) if compressed else text)


def test_audit_language():
	# Each text's grammar has its own clause: BCP 47 (3rd edition) and RFC 3066 (2nd edition).
	for language in (b"123", b"en-a", b"x"):
		_, record = check(png(((b"iTXt", itxt(language)),)), warning="11.3.3.4#language")
		assert ("11.3.3.4#language-rfc3066" in record.reached) == (language == b"123"), language
	# Exercise all ABNF branches and the required-content boundary of each branch.
	valid = (b"", b"en", b"zh-cmn-Hans-CN", b"abc-def-ghi-jkl-Latn-123-1996-a-aa-x-z", b"abcd", b"abcdefgh",
		b"de-CH-1901", b"sl-rozaj-biske", b"en-a-foo-b-bar-x-a", b"x-private", b"EN-gb-OED", b"i-klingon", b"sgn-BE-FR")
	invalid = (b"e", b"abcdefghi", b"en-", b"en--US", b"en-a", b"en-x", b"x", b"en-US-Latn", b"en-a-b", b"en_uk", b"en-\xff")
	for language in valid:
		check(png(((b"iTXt", itxt(language)),)), clean=True)
	for language in invalid:
		check(png(((b"iTXt", itxt(language)),)), warning="11.3.3.4#language")
	for language in (b"abcdefghi", b"en-", b"en--US", b"en_uk", b"en-\xff", b"123"):
		check(png(((b"iTXt", itxt(language)),)), warning="11.3.3.4#language-rfc3066")
	for language in valid + (b"en-a", b"en-US-Latn", b"en-a-b"):
		_, record = check(png(((b"iTXt", itxt(language)),)))
		assert "11.3.3.4#language-rfc3066" not in record.reached, language


def icc(tags=()):
	data = bytearray(132 + 12 * len(tags))
	data[16:20], data[36:40] = b"RGB ", b"acsp"
	struct.pack_into(">I", data, 128, len(tags))
	for i, payload in enumerate(tags):
		while len(data) % 4:
			data.append(0)
		struct.pack_into(">4sII", data, 132 + 12 * i, f"t{i:03}".encode(), len(data), len(payload))
		data += payload
	struct.pack_into(">I", data, 0, len(data))
	return bytes(data)


def test_audit_icc():
	audit = bytearray(128)
	audit[16:20] = b"RGB "
	def data(profile):
		return png(((b"iCCP", b"profile\0\0" + zlib.compress(profile)),))
	check(data(audit), warning="11.3.2.3#profile")
	check(data(icc()), clean=True)
	check(data(icc((b"text\0\0\0\0example\0",))), clean=True)
	# Every framing assertion has an independent witness; signatures alone cannot certify content.
	valid = icc((b"text\0\0\0\0example\0",))
	for pos, replacement in ((0, b"\0"*4), (36, b"nope"), (128, b"\xff"*4),
		(136, struct.pack(">I", 128)), (136, struct.pack(">I", 145)),
		(140, struct.pack(">I", 0xffffffff)), (140, struct.pack(">I", 4))):
		bad = bytearray(valid)
		bad[pos:pos + 4] = replacement
		check(data(bad), warning="11.3.2.3#profile")
	payload = b"text\0\0\0\0example\0"
	bad = bytearray(icc((payload, payload)))
	bad[144:148] = bad[132:136]
	check(data(bad), warning="11.3.2.3#profile")
	bad = bytearray(icc((payload, payload)))
	struct.pack_into(">I", bad, 148, 164)
	check(data(bad), warning="11.3.2.3#profile")


def tiff(order="<", entries=(), *, next_ifd=0, tail=b""):
	return (b"II*\0" if order == "<" else b"MM\0*") + struct.pack(order + "IH", 8, len(entries)) \
		+ b"".join(struct.pack(order + "HHII", *entry) for entry in entries) + struct.pack(order + "I", next_ifd) + tail


def test_audit_exif():
	check(png(((b"eXIf", b"II*\0"),)), warning="11.3.4.5#structure")
	for order in ("<", ">"):
		check(png(((b"eXIf", tiff(order)),)), warning="11.3.4.5#structure")
		check(png(((b"eXIf", tiff(order, ((0x0100, 4, 1, 1),))),)), clean=True)
		bad = (tiff(order, ((0x0100, 4, 2, 0xfffffff0),)), tiff(order, ((0x0100, 4, 1, 1),), next_ifd=8),
			tiff(order, ((0x0100, 4, 1, 1),), next_ifd=0xffffffff), tiff(order, ((0x8769, 4, 1, 0xffffffff),)),
			tiff(order, ((0x0100, 99, 1, 0),)), tiff(order)[:-1],
			tiff(order, ((0x0101, 4, 1, 1), (0x0100, 4, 1, 1))),
			tiff(order, ((0x0100, 4, 2, 27),), tail=b"\0" * 10))
		for profile in bad:
			check(png(((b"eXIf", profile),)), warning="11.3.4.5#structure")


def long_distance_stream():
	# Followup reproduction: 1024 bytes = two copies of a 512-byte prefix, declared 256-byte window.
	prefix = b"\0" + random.Random(1950).randbytes(511)
	raw = prefix * 2
	stream = b"\x08\x1d" + zlib.compress(raw, 9)[2:]
	return raw, stream


def test_audit_window(*, include_fixture=True):
	raw, stream = long_distance_stream()
	img, record = check(png(stream=stream, width=1023, color=0), warning="rfc1950-2.2#window-exceeded")
	assert tuple(img.samples[0::4]) == tuple(raw[1:])
	assert expectations(record) == both("may-reject")
	if include_fixture:
		fixture = ROOT / "decode/i_zlib-cinfo-0-long-distance.png"
		assert fixture.is_file()
		check(fixture.read_bytes(), warning="rfc1950-2.2#window-exceeded")
	# A subsequent bad Adler cannot erase the recovered window fault; the complete data are decoded.
	bad = stream[:-1] + bytes((stream[-1] ^ 1,))
	img, _ = check(png(stream=bad, width=1023, color=0), warning="rfc1950-2.2#window-exceeded", error="rfc1950-2.3#adler32")
	assert tuple(img.samples[0::4]) == tuple(raw[1:])


def test_exit_paths():
	# Truncation is recorded before a later invalid filter or short-row rejection can interrupt decoding.
	for raw, fatal in ((b"\x05\x7f", "9.2#filter-types"), (b"\0", "13.1#data-short")):
		_, record = check(png(stream=zlib.compress(raw)[:-4], color=0), warning="10.1#zlib-check", no_image=fatal)
		assert "rfc1950-2.2#end-in-adler32" in record.reached
	# Both the API and CLI preserve accumulated faults on unexpected exceptions, including MemoryError.
	data = png(stream=zlib.compress(b"\0\x7f")[:-4], color=0)
	original = refpng._scanlines
	with tempfile.TemporaryDirectory() as td:
		src, dest = Path(td) / "in.png", Path(td) / "out.pam"
		src.write_bytes(data)
		try:
			for error_type in (RuntimeError, MemoryError):
				def broken(*args):
					raise error_type("deliberate failure after inflate")
				refpng._scanlines = broken
				out = io.StringIO()
				try:
					with redirect_stdout(out):
						refpng.main([str(src), str(dest)])
				except error_type as e:
					assert "10.1#zlib-check" in e.diagnostics.reached
				else:
					raise AssertionError("unexpected exception was swallowed")
				assert "WARN [10.1#zlib-check]" in out.getvalue() and "OK " not in out.getvalue()
				assert not dest.exists()
		finally:
			refpng._scanlines = original
		out = io.StringIO()
		with redirect_stdout(out):
			assert refpng.main([str(src), str(dest)]) == 0
		assert out.getvalue().endswith("OK 1 1 4 8\n") and dest.exists()
		bad = png(stream=zlib.compress(b"\x05\x7f")[:-4], color=0)
		src.write_bytes(bad)
		out = io.StringIO()
		with redirect_stdout(out):
			assert refpng.main([str(src), str(dest)]) == 1
		assert "WARN [10.1#zlib-check]" in out.getvalue() and "ERR [9.2#filter-types]" in out.getvalue()
		assert "OK " not in out.getvalue()


def test_content_types():
	# Keyword/encoding/compression constraints belong to the types, including inside nested compression.
	for name, body, id in ((b"tEXt", b" key\0text", "11.3.3.1#keyword-spaces"),
		(b"zTXt", b"key\0\0" + zlib.compress(b"text\0"), "11.3.3.3#null"),
		(b"iTXt", itxt(b"en", b"\xc0\xaf", compressed=True), "11.3.3.4#utf8"),
		(b"iTXt", itxt(b"en", b"a\0b"), "11.3.3.4#null"),
		(b"iTXt", itxt(b"en", translated=b"\xff"), "11.3.3.4#utf8"),
		(b"zTXt", b"key\0\0\x78\x9c\x07", "11.3.3.3#zlib"),
		(b"zTXt", b"key\0\0" + zlib.compress(b"text") + b"extra", "11.3.3.3#zlib")):
		_, record = check(png(((name, body),)), warning=id)
		assert_reported(record)
	# A recovered inflate condition survives a subsequent ancillary stream failure and image rejection.
	_, stream = long_distance_stream()
	bad = stream[:-1] + bytes((stream[-1] ^ 1,))
	_, record = check(png(((b"zTXt", b"key\0\0" + bad),), raw=b"\x05\0\0\0"),
		warning="rfc1950-2.2#window-exceeded", no_image="9.2#filter-types")
	assert "rfc1950-2.3#adler32" in record.reached
	assert "11.3.3.3#zlib" in {id for id, _ in record.warnings}
	check(png(((b"iTXt", itxt(b"en", method=17)),)), clean=True)


def test_owner_policies():
	# The sample mask is required under both editions.
	check(png(((b"tRNS", b"\xff\x7f"), (b"bKGD", b"\xff\x7f")), raw=b"\0\x7f", color=0),
		pixels=(127, 127, 127, 0), clean=True)
	# CRC failure: the first interpretation drops ancillary tRNS; the second uses it as stored, like a recomputed CRC.
	valid = png(((b"tRNS", b"\0\x12\0\x34\0\x56"),))
	bad = bytearray(valid)
	pos = bad.index(b"tRNS") + 4 + 6
	bad[pos] ^= 1
	check(bytes(bad), warning="13.1#crc-ancillary", pixels=(18, 52, 86, 255), choices=((("crc", "dropped"),), (("crc", "ignored"),)))
	assert interpretations(bytes(bad))[(("crc", "ignored"),)] == (18, 52, 86, 0)
	check(valid, pixels=(18, 52, 86, 0))
	check(png()[:-12], warning="11.2#iend-present")
	check(png() + b"extra", warning="11.2#iend-last")
	check(png(raw=b"\0\x12\x34\x56extra"), warning="13.1#data-long")
	check(png(raw=b"\0\x12"), no_image="13.1#data-short")
	check(png(((b"acTL", b"\0" * 8),)), warning="11.3.6.1#num-frames-zero", pixels=(18, 52, 86, 255))


def test_small_pixels():
	# Independent sample packing across every legal color/depth combination, and all five inverse filters.
	for color, depths in ((0, (1, 2, 4, 8, 16)), (2, (8, 16)), (3, (1, 2, 4, 8)), (4, (8, 16)), (6, (8, 16))):
		for depth in depths:
			nch = refpng.CHANNELS[color]
			maxval = (1 << depth) - 1
			width, height = 3, 2
			rng = random.Random(color * 100 + depth)
			rows = [[rng.randrange(maxval + 1) for _ in range(width * nch)] for _ in range(height)]
			if color == 3:
				rows = [[v % min(4, maxval + 1) for v in row] for row in rows]
			palette = bytes((13, 27, 53, 81, 127, 195, 0, 255, 99, 255, 11, 7))[:3 * min(4, maxval + 1)]
			metadata = ((b"PLTE", palette),) if color == 3 else ()
			pixels = []
			for row in rows:
				for x in range(width):
					v = row[x * nch:(x + 1) * nch]
					outmax = 65535 if depth == 16 else 255
					if color == 3:
						pixels.extend((*palette[3 * v[0]:3 * v[0] + 3], 255))
					elif color in (0, 4):
						g = v[0] * (outmax // maxval)
						pixels.extend((g, g, g, v[1] if color == 4 else outmax))
					else:
						pixels.extend((*v[:3], v[3] if color == 6 else outmax))
			for interlace in (0, 1):
				passes = refpng.ADAM7 if interlace else ((0, 0, 1, 1),)
				for filter_type in range(5):
					raw = bytearray()
					for x0, y0, dx, dy in passes:
						if x0 >= width or y0 >= height:
							continue
						prev = None
						for y in range(y0, height, dy):
							v = [s for x in range(x0, width, dx) for s in rows[y][x*nch:(x+1)*nch]]
							if depth == 16:
								line = struct.pack(">" + "H" * len(v), *v)
							elif depth == 8:
								line = bytes(v)
							else:
								bits = "".join(f"{s:0{depth}b}" for s in v)
								bits += "1" * (-len(bits) % 8)
								line = int(bits, 2).to_bytes(len(bits)//8, "big")
							prev = prev if prev is not None else bytes(len(line))
							bpp = max(1, (nch * depth + 7)//8)
							encoded = bytearray()
							for i, value in enumerate(line):
								a, b, c = (line[i-bpp] if i >= bpp else 0), prev[i], (prev[i-bpp] if i >= bpp else 0)
								p = a + b - c
								paeth = min(((abs(p-a), 0, a), (abs(p-b), 1, b), (abs(p-c), 2, c)))[2]
								pred = (0, a, b, (a+b)//2, paeth)[filter_type]
								encoded.append((value - pred) & 255)
							raw += bytes((filter_type,)) + encoded
							prev = line
					check(png(metadata, raw=bytes(raw), width=width, height=height, color=color, depth=depth, interlace=interlace),
						pixels=pixels, clean=True)


def require_memory_scope():
	"""Re-exec under the owner's memory limit; a marker alone cannot bypass the cgroup check."""
	group = next(line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
	path = Path("/sys/fs/cgroup") / group.lstrip("/")
	bounded = False
	for parent in (path, *path.parents):
		if parent == Path("/sys/fs"):
			break
		try:
			memory = (parent / "memory.max").read_text().strip()
			swap = (parent / "memory.swap.max").read_text().strip()
		except FileNotFoundError:
			continue
		if memory != "max" and int(memory) <= 16 * 1024**3 and swap == "0":
			bounded = True
	if bounded:
		return
	assert "--in-scope" not in sys.argv, "systemd scope did not establish MemoryMax=16G and MemorySwapMax=0"
	os.execvp("systemd-run", ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=16G", "-p", "MemorySwapMax=0",
		"uv", "run", "python", str(Path(__file__).resolve()), *sys.argv[1:], "--in-scope"])


def load_baseline(directory):
	"""Isolate the baseline modules, using its own recorder when its catalog is supplied."""
	names = ("inflate", "pngfields", "refpng")
	if (directory / "pngdiagnostics.py").exists() and (directory / "spec/clauses.tsv").exists():
		names = ("pngdiagnostics", *names)
	saved = {name: sys.modules.get(name) for name in names}
	try:
		for name in names:
			path = directory / (name + ".py")
			if name == "pngfields" and not path.exists():
				continue
			spec = importlib.util.spec_from_file_location(name, path)
			obj = importlib.util.module_from_spec(spec)
			# Dataclasses resolve their module during class construction.
			sys.modules[name] = obj
			spec.loader.exec_module(obj)
		return sys.modules["refpng"]
	finally:
		for name, module in saved.items():
			if module is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = module


def compare_manifest(report_path, baseline=None):
	"""Decode every file once and compare the manifest's derived columns with this decode's (expect.row_for):
	`images` (every interpretation's choices and digest), `reaches`, `clause`, `strict` and each lens's
	`conforms` and `expect`. A difference in `images` fails the run; the others are listed."""
	import csv
	from collections import Counter
	import expect
	with (ROOT / "decode/manifest.tsv").open(newline="") as f:
		rows = {r["file"]: r for r in csv.DictReader(f, delimiter="\t")}
	files = sorted((ROOT / "decode").rglob("*.png"))
	assert {str(p.relative_to(ROOT / "decode")) for p in files} == set(rows), "manifest and PNG file sets differ"
	derived = [k for k in expect.FIELDS if k not in ("id", "file", "description")]
	result = {"files": len(files), "image_differences": [], "column_differences": [],
		"baseline_pixel_differences": [], "baseline_diagnostic_differences": []}
	def run_one(module, data):
		try:
			decoded = module.decode(data)
		except module.NoImage as e:
			record, interpretations = e.diagnostics, []
		else:
			record = decoded.diagnostics
			interpretations = [(",".join(f"{t}:{o}" for t, o in i.choices), module.digest(i.image)) for i in decoded.interpretations]
		if module is refpng:
			assert_reported(record)
		return {"diagnostics": [l for l in stdout(record).splitlines() if not l.startswith("INFO ")], "reached": sorted(record.reached),
			"interpretations": interpretations}
	for index, file in enumerate(files):
		name = str(file.relative_to(ROOT / "decode"))
		row = rows[name]
		fresh = expect.row_for(file, ROOT / "decode")
		where = {"file": name}
		if fresh["images"] != row["images"]:
			result["image_differences"].append(where | {"expected": row["images"], "actual": fresh["images"]})
		changed = {k: {"expected": row[k], "actual": fresh[k]} for k in derived if k != "images" and fresh[k] != row[k]}
		if changed:
			result["column_differences"].append(where | changed)
		if baseline:
			data = file.read_bytes()
			actual, old = run_one(refpng, data), run_one(baseline, data)
			if old["interpretations"] != actual["interpretations"]:
				result["baseline_pixel_differences"].append(where | {"before": old, "after": actual})
			if (old["diagnostics"], old["reached"]) != (actual["diagnostics"], actual["reached"]):
				result["baseline_diagnostic_differences"].append(where | {"before": old, "after": actual,
					"added": list((Counter(actual["diagnostics"]) - Counter(old["diagnostics"])).elements()),
					"removed": list((Counter(old["diagnostics"]) - Counter(actual["diagnostics"])).elements())})
		if index % 25 == 0:
			print(f"Compared {index + 1}/{len(files)} files", flush=True)
		if report_path:
			report_path.write_text(json.dumps(result, indent=2) + "\n")
	print(json.dumps(result, indent=2))
	if baseline is None:
		assert not result["image_differences"], "manifest image differences require review; see report"
	assert not result["baseline_pixel_differences"], "baseline pixel differences require review; see report"


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--manifest", action="store_true")
	parser.add_argument("--report", type=Path)
	parser.add_argument("--baseline", type=Path)
	parser.add_argument("--in-scope", action="store_true", help=argparse.SUPPRESS)
	args = parser.parse_args()
	require_memory_scope()
	for test in (test_diagnostic_invariant, test_layout_invariant, test_audit_singleton, test_audit_uint31,
		test_audit_srgb, test_audit_language, test_audit_icc, test_audit_exif, test_audit_window,
		test_exit_paths, test_content_types, test_owner_policies, test_small_pixels, test_independent_chunk_faults,
		test_value_then_layout_fault, test_dependent_choice, test_dependent_count, test_dependent_compression,
		test_embedded_value_faults, test_fault_classification_and_dispatch,
		test_hdist_policy, test_corrupt_image_tail, test_structure_before_crc, test_diagnostic_severity,
		test_cicp_code_points, test_conformance_naming, test_extensions, test_exif,
		test_idot, test_interpretations, test_conformance_mutations):
		test()
		print(test.__name__ + ": passed", flush=True)
	if args.manifest:
		compare_manifest(args.report, load_baseline(args.baseline) if args.baseline else None)
	print("All requested checks passed")


def test_independent_chunk_faults():
	_, stream = long_distance_stream()
	_, record = check(png(((b"zTXt", b"Two  spaces\0\0" + stream),)))
	ids = {id for id, _ in record.warnings}
	assert {"11.3.3.1#keyword-spaces", "rfc1950-2.2#window-exceeded"} <= ids, ids
	assert expectations(record) == both("may-reject")


def test_value_then_layout_fault():
	# r_ztxt-len-1's complete zTXt body: empty keyword and null, with no method byte.
	_, record = check(png(((b"zTXt", bytes.fromhex("00")),)))
	ids = {id for id, _ in record.warnings}
	assert ids == {"11.3.3.1#keyword-length", "11.3.3.3#separator"}, ids


def parse_faults(schema, data, *, state=None):
	d = state if state is not None else refpng._State(Diagnostics())
	parser = Parser(d, b"teST", data, schema)
	try:
		parser.parse()
	except Ignored:
		assert_reported(d.diagnostics)
	else:
		raise AssertionError("faulted fields reached the handler boundary")
	return parser, {id for id, _ in d.diagnostics.warnings}


def test_dependent_choice():
	_, stream = long_distance_stream()
	# The method's width is known despite the unusable flag. Language and translated text remain independent.
	body = b"Comment\0\x02\xff123\0\xff\0" + stream
	_, record = check(png(((b"iTXt", body),)))
	assert {id for id, _ in record.warnings} == {"11.3.3.4#flag", "11.3.3.4#language", "11.3.3.4#language-rfc3066", "11.3.3.4#utf8"}
	# All branches consume the remainder: an invalid sample depth cannot create an invented entry-length fault.
	_, record = check(png(((b"sPLT", b"palette\0\x07garbage"),)))
	assert {id for id, _ in record.warnings} == {"11.3.4.4#depth"}
	# Branches with different extents leave the tail unlocatable. Never choose a branch using the invalid bits.
	schema = refpng.layout(("selector", refpng.enum((0, 1), "11.3.3.4#flag")),
		("dependent", Choice(lambda p: p.values.get("selector"), {0: BYTE, 1: UINT16})),
		("tail", refpng.enum((0,), "11.3.4.3#unit")))
	parser, ids = parse_faults(schema, b"\x02\xff\xff\xff")
	assert ids == {"11.3.3.4#flag"} and parser.pos == 1


def test_dependent_count():
	schema = refpng.layout(("count", UINT31), ("entries", Repeat(UINT31, lambda p: p.values["count"])),
		("tail", refpng.enum((0,), "11.3.4.3#unit")))
	parser, ids = parse_faults(schema, b"\x80\0\0\0" + b"\xff" * 5)
	assert ids == {"7.1#uint31"} and parser.pos == 4
	# A valid count inconsistent with the available bytes is a layout fault; no partial entries are interpreted.
	parser, ids = parse_faults(schema, b"\0\0\0\x02\xff\xff")
	assert ids == {"13.2#known-length"} and parser.pos == 4
	# A dependent allowed-value rule is resolved before reading its fixed-width field; the independent tail survives.
	schema = refpng.layout(("selector", refpng.enum((0,), "11.3.3.4#flag")),
		("dependent", UInt(1, "11.3.3.4#method", lambda p: (p.values["selector"],))),
		("tail", refpng.enum((0,), "11.3.4.3#unit")))
	parser, ids = parse_faults(schema, b"\xff\xff\xff")
	assert ids == {"11.3.3.4#flag", "11.3.4.3#unit"} and parser.pos == 3


def test_dependent_compression():
	_, stream = long_distance_stream()
	cases = ((b"zTXt", b"Comment\0\x01" + stream, {"11.3.3.3#method"}),
		(b"iCCP", b"profile\0\x01" + stream, {"11.3.2.3#method"}),
		(b"iTXt", b"Comment\0\x01\x01" + b"123\0\xff\0" + stream,
			{"11.3.3.4#method", "11.3.3.4#language", "11.3.3.4#language-rfc3066", "11.3.3.4#utf8"}))
	for name, body, expected in cases:
		_, record = check(png(((name, body),)))
		assert {id for id, _ in record.warnings} == expected
		assert not any(id.startswith("rfc") for id, _ in record.warnings)
	# An uncompressed method is ignored, so it does not disable UTF-8 validation of the text.
	_, record = check(png(((b"iTXt", itxt(b"en", b"\xff", method=255)),)))
	assert {id for id, _ in record.warnings} == {"11.3.3.4#utf8"}


def test_embedded_value_faults():
	# Header values, independent tag entries and the overlap rule do not short-circuit one another.
	data = bytearray(icc((b"text\0\0\0\0example\0",) * 2))
	data[:4] = b"\0" * 4
	data[36:40] = b"nope"
	struct.pack_into(">I", data, 136, 0xffffffff)
	struct.pack_into(">I", data, 148, 0xfffffffc)
	_, record = check(png(((b"iCCP", b"profile\0\0" + zlib.compress(data)),)))
	messages = [msg for id, msg in record.warnings if id == "11.3.2.3#profile"]
	assert len(messages) == 4, messages
	assert any("declared size" in msg for msg in messages) and any("signature" in msg for msg in messages)
	assert sum("invalid extent" in msg for msg in messages) == 2
	# A bad field type cannot supply a width, but the next 12-byte TIFF entry is independently located.
	data = tiff(entries=((0x0100, 99, 1, 0), (0x0101, 4, 2, 0xfffffff0)))
	_, record = check(png(((b"eXIf", data),)))
	messages = [msg for id, msg in record.warnings if id == "11.3.4.5#structure"]
	assert len(messages) == 2 and "no defined width" in messages[0] and "exceeds profile bounds" in messages[1], messages
	# Invalid Exif pointers are not followed; later directory entries still get checked.
	data = tiff(entries=((0x8769, 3, 1, 0xffff), (0x8825, 4, 2, 38)), tail=b"\xff" * 8)
	_, record = check(png(((b"eXIf", data),)))
	messages = [msg for id, msg in record.warnings if id == "11.3.4.5#structure"]
	assert len(messages) == 2 and "not LONG/IFD" in messages[0] and "must have one value" in messages[1], messages
	# A short embedded header is a layout failure in that bounded content; it cannot cause an out-of-bounds read.
	check(png(((b"iCCP", b"profile\0\0" + zlib.compress(b"short")),)), warning="11.3.2.3#profile")


def test_fault_classification_and_dispatch():
	import inspect
	assert inspect.signature(Parser.check).parameters["kind"].default is inspect.Parameter.empty
	assert inspect.signature(Parser.fail).parameters["kind"].default is inspect.Parameter.empty
	for node in ast.walk(ast.parse((ROOT / "pngfields.py").read_text())):
		if isinstance(node, ast.Call) and (isinstance(node.func, ast.Attribute) and node.func.attr in ("check", "fail")
				or isinstance(node.func, ast.Name) and node.func.id == "check"):
			assert any(k.arg == "kind" for k in node.keywords), node.lineno
	# Several faults in one field, then another independent field: all observations belong to the record.
	_, record = check(png(((b"tEXt", b" \x01  \0\0\x01"),)))
	assert {id for id, _ in record.warnings} == {"11.3.3.1#keyword-latin1", "11.3.3.1#keyword-spaces", "11.3.3.2#null", "11.3.3.2#latin1"}
	# A layout failure stops the scope immediately, including independent-looking bytes after an incomplete entry.
	schema = refpng.layout(("keyword", Text("keyword", True)), ("entries", Repeat(UINT16)))
	parser, ids = parse_faults(schema, b"\0\xff")
	assert ids == {"11.3.3.1#keyword-length", "13.2#known-length"} and parser.pos == 1
	# An array's cardinality limit is a value rule when all entry boundaries remain known.
	schema = refpng.layout(("entries", Repeat(UINT31, maximum=1)))
	parser, ids = parse_faults(schema, struct.pack(">II", 0x80000000, 0x80000001))
	assert ids == {"13.2#known-length", "7.1#uint31"} and parser.pos == 8
	assert parser.d.diagnostics.fault_count == 3
	# A new occurrence of a deduplicated diagnostic still makes its field and chunk unusable.
	d = refpng._State(Diagnostics())
	schema = refpng.layout(("value", UINT31))
	for _ in range(2):
		parse_faults(schema, b"\xff" * 4, state=d)
	assert len(d.diagnostics.events) == 1 and d.diagnostics.fault_count == 2
	# Recoverable stream faults also prevent dispatch even when decompression and content validation complete.
	calls = []
	original = refpng.CHUNKS[b"zTXt"]
	def handler(d, f):
		calls.append(f)
	refpng.CHUNKS[b"zTXt"] = refpng.Chunk(handler, original.layout)
	try:
		for _ in range(2):
			check(png(((b"zTXt", b"\0\0" + zlib.compress(b"valid text")),)), warning="11.3.3.1#keyword-length")
		assert not calls
		rng = random.Random(512)
		prefix = bytes(rng.randrange(32, 127) for _ in range(512))
		stream = b"\x08\x1d" + zlib.compress(prefix * 2, 9)[2:]
		body = b"Comment\0\0" + stream
		_, record = check(png(((b"zTXt", body), (b"zTXt", body))))
		assert {id for id, _ in record.warnings} == {"rfc1950-2.2#window-exceeded"}
		assert record.fault_count >= 2 and not calls
	finally:
		refpng.CHUNKS[b"zTXt"] = original
	# Fatal critical parsing still stops at its first invalid value.
	_, record = check(png(width=0x80000000, height=0x80000001), no_image="11.2.1#dimensions-range")
	assert len(record.events) == 1 and "2147483648" in record.events[0].message


HDIST31_PNG = bytes.fromhex(
	"89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000003a49444154789c059e010400"
	"2010c40ec0000cc0000000000000000000000000000000000000000000000000000000005c55555555555555fdb00300"
	"68003d272612710000000049454e44ae426082"
)

CORRUPT_TAIL_PNG = bytes.fromhex(
	"89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
	"00000012494441547801000400fbff0012345603120000f8009d5c5495da0000000049454e44ae426082")


def test_hdist_policy():
	# RFC 1951 3.3: "A compliant decompressor must accept the full range of possible values", and HDIST's is 1-32.
	_, record = check(HDIST31_PNG, clean=True)
	assert "rfc1951-3.2.7#hdist-over-30" in record.reached
	assert expectations(record) == both("display")


def test_corrupt_image_tail():
	_, record = check(CORRUPT_TAIL_PNG, pixels=(18, 52, 86, 255))
	assert "rfc1951-3.2.3#distance-too-far" in record.reached
	assert expectations(record) == both("may-reject")
	# A bad block header, including one whose table construction fails, has the same completion policy.
	def packed(fields):
		bits = [value >> i & 1 for value, width in fields for i in range(width)]
		return bytes(sum(bit << i for i, bit in enumerate(bits[j:j+8])) for j in range(0, len(bits), 8))
	oversubscribed = packed(((1, 1), (2, 2), (0, 5), (0, 5), (0, 4), *((1, 3),) * 4))
	def stream(raw, tail):
		return b"\x78\x01\0" + struct.pack("<HH", len(raw), len(raw) ^ 65535) + raw + tail
	for tail, cause in ((b"\x07", "rfc1951-3.2.3#btype-reserved"),
		(oversubscribed, "rfc1951-3.2.2#over-subscribed")):
		for raw in (b"\0\x12\x34\x56", b"\0\x12\x34\x56extra"):
			_, record = check(png(stream=stream(raw, tail)),
				warning="10.1#zlib-check", pixels=(18, 52, 86, 255))
			assert any(e.id == "10.1#zlib-check" and e.cause == cause for e in record.events)
			assert not any(e.level == "ERR" for e in record.events)
			assert expectations(record) == both("may-reject")
		check(png(stream=stream(b"\0\x12\x34", tail)), no_image=cause)
	# A 2x2 one-bit Adam7 image has three nonempty passes and six bytes, including their filters.
	check(png(stream=stream(b"\0\0\0\0\0\0", b"\x07"), color=0, depth=1, width=2, height=2, interlace=1),
		warning="10.1#zlib-check", pixels=(0, 0, 0, 255) * 4)
	check(png(stream=stream(b"\0\0\0\0\0", b"\x07"), color=0, depth=1, width=2, height=2, interlace=1),
		no_image="rfc1951-3.2.3#btype-reserved")
	# A wrong Adler-32 is not a fault after the image: it is recorded at its own clause's level (reject).
	valid = zlib.compress(b"\0\x12\x34\x56")
	_, record = check(png(stream=valid[:-1] + bytes((valid[-1] ^ 1,))), error="rfc1950-2.3#adler32", pixels=(18, 52, 86, 255))
	assert "10.1#zlib-check" not in record.reached


def corrupt_crc(name, body):
	data = bytearray(chunk(name, body))
	data[-1] ^= 1
	return bytes(data)


def test_structure_before_crc():
	"""A chunk's type and position are checked before its type letters and CRC, so a bad CRC cannot hide them."""
	header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
	idat = chunk(b"IDAT", zlib.compress(b"\0\x12\x34\x56"))
	indexed = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
	used = ((("crc", "ignored"),),)
	for data, id, outcome in (
		(refpng.SIGNATURE + header + corrupt_crc(b"IHDR", header[8:-4]), "11.2#one-ihdr", "11.2#idat-present"),
		(refpng.SIGNATURE + header + corrupt_crc(b"ABCD", b""), "5.4#unknown-critical", "11.2#idat-present"),
		(refpng.SIGNATURE + corrupt_crc(b"IDAT", b""), "11.2#ihdr-first", "11.2#ihdr-first"),
		(refpng.SIGNATURE + header + idat + chunk(b"tEXt", b"key\0text") + corrupt_crc(b"IDAT", b""), "11.2.3#consecutive", used),
		(refpng.SIGNATURE + header + corrupt_crc(b"IEND", b""), "11.2#idat-present", "11.2#idat-present"),
		(refpng.SIGNATURE + header + corrupt_crc(b"A\xffCD", b""), "5.4#unknown-critical", "11.2#idat-present"),
		(refpng.SIGNATURE + corrupt_crc(b"a\xffCD", b""), "11.2#ihdr-first", "11.2#ihdr-first"),
		(refpng.SIGNATURE + corrupt_crc(b"\nfoo", b""), "11.2#ihdr-first", "11.2#ihdr-first"),
		(refpng.SIGNATURE + corrupt_crc(b"IHDR", b""), "11.2.1#length", "11.2.1#length"),
		(refpng.SIGNATURE + indexed + corrupt_crc(b"PLTE", b"\0"), "11.2.2#invalid-indexed", "11.2.2#invalid-indexed"),
		(refpng.SIGNATURE + indexed + corrupt_crc(b"PLTE", b""), "11.2.2#invalid-indexed", "11.2.2#invalid-indexed"),
		(refpng.SIGNATURE + indexed + corrupt_crc(b"IDAT", b""), "11.2.2#required", "11.2.2#required")):
		if isinstance(outcome, str):
			_, record = check(data, no_image=outcome)
		else:
			_, record = check(data, choices=outcome, pixels=(18, 52, 86, 255))
		ids = [e.id for e in record.events]
		later = [ids.index(c) for c in ("13.1#crc-critical", "13.1#crc-ancillary", "5.3#type-letters") if c in ids]
		assert id in ids and all(ids.index(id) < i for i in later), (id, ids)
		assert expectations(record, no_image=isinstance(outcome, str)) == both("must-reject"), (id, expectations(record))


def test_diagnostic_severity():
	allowed = {"INFO": {"none"}, "WARN": {"recover", "either", "still", "unaffected"}, "ERR": {"reject", "either"}}
	for id, consequence in CONSEQUENCES.items():
		for level, kinds in allowed.items():
			record = Diagnostics()
			try:
				record.record(id, level, "deliberate severity probe" if level != "INFO" else "")
			except AssertionError:
				assert consequence not in kinds, (id, level, consequence)
				assert not record.events and record.fault_count == 0
			else:
				assert consequence in kinds, ("mis-severity accepted", id, level, consequence)




def test_cicp_code_points():
	"""Every ColourPrimaries and TransferCharacteristics value, against H.273 (07/2024) Tables 2 and 3."""
	reserved = {
		"11.3.2.6#primaries": {0, 3, *range(13, 22), *range(23, 256)},
		"11.3.2.6#transfer": {0, 3, *range(19, 256)},
	}
	for clause, bad in reserved.items():
		for value in range(256):
			fields = (value, 1, 0, 1) if clause.endswith("primaries") else (1, value, 0, 1)
			data = png(((b"cICP", bytes(fields)),))
			if value in bad:
				_, record = check(data, warning=clause)
				assert "11.3.2.6#cicp" not in record.reached, "a cICP with a reserved code point is ignored"
				assert expectations(record) == both("recover")
			else:
				_, record = check(data, clean=True)
				assert "11.3.2.6#cicp" in record.reached
	# Both fields reserved: both faults are reported, and the chunk is still ignored.
	_, record = check(png(((b"cICP", bytes((200, 200, 0, 1))),)))
	assert {id for id, _ in record.warnings} == {"11.3.2.6#primaries", "11.3.2.6#transfer"}, record.warnings


def pcal(*, name=b"calibration", x0=-1, x1=255, equation=0, count=2, unit=b"K", parameters=b"0\0+1.e2"):
	return name + b"\0" + struct.pack(">iiBB", x0, x1, equation, count) + unit + (b"\0" + parameters if count else b"")


def extension_cases():
	"""One falsifiable example of each new extension fault clause."""
	bodies = {
		b"oFFs": struct.pack(">iiB", -2147483648, 2147483647, 1),
		b"pCAL": pcal(), b"sCAL": b"\1.5\0+2.e-9999999999999999999999999999999",
		b"gIFg": b"\3\1\xff\xff", b"gIFx": b"APP NAME\xff\0\xff\0\xffapplication",
		b"sTER": b"\0", b"gIFt": struct.pack(">iiII8B", -5, -6, 9, 1, 1, 1, 255, 0, 0, 0, 255, 0) + b"text",
	}
	def image(name, body, **kw):
		return png(((name, body),), width=9, raw=b"\0" + b"\x12\x34\x56" * 9, **kw)
	cases = []
	for name, section in ((b"oFFs", "4.1"), (b"pCAL", "4.2"), (b"sCAL", "4.3"),
		(b"gIFg", "4.4"), (b"gIFx", "4.5"), (b"sTER", "4.6"), (b"gIFt", "7.1")):
		cases.append((f"ext-{section}#layout", image(name, b"")))
	for name, body in bodies.items():
		if name in (b"oFFs", b"pCAL", b"sCAL", b"sTER"):
			cases.append(("ext-3#single", image(name, body, after=((name, body),))))
			cases.append(("ext-3#before-idat", png(after=((name, body),))))
	for clause, name, body in (
		("ext-4.1#unit", b"oFFs", bodies[b"oFFs"][:-1] + b"\2"),
		("ext-4.2#name", b"pCAL", pcal(name=b"bad  name")),
		("ext-4.2#range", b"pCAL", pcal(x0=2, x1=2)),
		("ext-4.2#equation", b"pCAL", pcal(equation=4)),
		("ext-4.2#parameters", b"pCAL", pcal(count=3)),
		("ext-4.2#parameters", b"pCAL", pcal(parameters=b"0")),
		("ext-4.2#unit", b"pCAL", pcal(unit=b"\n")),
		("ext-4.3#unit", b"sCAL", b"\0" + b"1\0" + b"1"),
		("ext-4.3#positive", b"sCAL", b"\1-1\0" + b"1"),
		("ext-2.2#float", b"sCAL", b"\1nan\0" + b"1"),
		("ext-4.4#disposal", b"gIFg", b"\4\0\0\0"),
		("ext-4.4#input", b"gIFg", b"\0\2\0\0"),
		("ext-7.1#text", b"gIFt", bodies[b"gIFt"] + b"\x7f"),
		("ext-4.5#identifier", b"gIFx", b"\x1f" + bodies[b"gIFx"][1:]),
		("ext-4.6#mode", b"sTER", b"\2")):
		cases.append((clause, image(name, body)))
	cases.append(("ext-4.6#width", png(((b"sTER", b"\0"),))))
	return bodies, cases


def test_conformance_naming():
	for name in (b"vpAG", b"vpAg"):
		check(png(((name, b"opaque"),)), clean=True)
	for name, clause in ((b"vpag", "5.4#reserved-bit"), (b"vPAG", "5.7.2#undefined-public")):
		_, record = check(png(((name, b"opaque"),)), warning=clause, pixels=(18, 52, 86, 255))
		assert expectations(record) == both("display")
		if name == b"vpag":
			assert "13.5#reserved-bit" in record.reached
	# Naming is checked even when the CRC subsequently disables content dispatch.
	data = png()
	_, record = check(data[:33] + corrupt_crc(b"vPAG", b"") + data[33:], warning="5.7.2#undefined-public")
	assert "13.1#crc-ancillary" in record.reached
	check(png(((b"VPAG", b""),)), error="5.4#unknown-critical", choices=((("critical", "ignored"),),))
	# A chunk is known or unknown: the chunks the 3rd edition and the extensions define are known, and parsed.
	for name in (b"acTL", b"fcTL", b"fdAT", b"cICP", b"mDCV", b"cLLI", b"iDOT", b"eXIf"):
		_, record = check(png(((name, b""),)))
		assert faults(record) and "5.7.2#undefined-public" not in record.reached and "5.4#unknown-ancillary" not in record.reached, name
	# iDOT's public name is a coverage note of its extension, not a naming fault.
	_, record = check(idot_png(), clean=True)
	assert "idot#public-name" in record.reached


def test_extensions():
	bodies, cases = extension_cases()
	for name, body in bodies.items():
		check(png(((name, body),), width=9, raw=b"\0" + b"\x12\x34\x56" * 9), clean=True)
	for clause, data in cases:
		check(data, warning=clause)
	for name, clause in ((b"dSIG", "ext-5.1#dsig"), (b"fRAc", "ext-5.2#frac")):
		_, record = check(png(((name, b"\xff\0anything"),)), clean=True)
		assert clause in record.reached
	for name in (b"gIFg", b"gIFx", b"gIFt"):
		check(png(((name, bodies[name]), (name, bodies[name])), after=((name, bodies[name]),)), clean=True)
	for equation, count in enumerate((2, 3, 3, 4)):
		check(png(((b"pCAL", pcal(equation=equation, count=count, parameters=b"\0".join([b"-1"] * count))),)), clean=True)
	for name in (b"", b"a" * 80, b" leading", b"trailing ", b"two  spaces", b"control\x7f"):
		check(png(((b"pCAL", pcal(name=name)),)), warning="ext-4.2#name")
	for width in (b"0", b"-0", b"+0.000e100", b"-1e-100000"):
		for body in (b"\1" + width + b"\0" + b"1", b"\1" + b"1\0" + width):
			check(png(((b"sCAL", body),)), warning="ext-4.3#positive")
	valid = (b"1", b"+1", b"1.", b".1", b"1.5", b"1e0", b"1.E+2", b".1e-99999999999999999999999")
	invalid = (b"", b".", b"+", b"1e", b"1e+", b"1.e.2", b" 1", b"1 ", b"1\n", b"1_0", b"nan", b"inf", b"0x1", b"1f", b"\xff", b"1\0")
	for value in valid + invalid:
		check(png(((b"sCAL", b"\1" + b"1\0" + value),)), clean=value in valid,
			warning=None if value in valid else "ext-2.2#float")
	check(png(((b"pCAL", pcal(x0=-2147483648, x1=2147483647)),)), clean=True)
	for position in (8, 12):
		body = bytearray(bodies[b"gIFt"])
		struct.pack_into(">I", body, position, 0x80000000)
		check(png(((b"gIFt", bytes(body)),)), warning="7.1#uint31")
	_, record = check(png(((b"sCAL", b"\1bad\0-1"),)))
	assert {"ext-2.2#float", "ext-4.3#positive"} <= record.reached
	# Independent cross-field checks survive invalid text or an unusable equation selector.
	_, record = check(png(((b"pCAL", pcal(name=b"", x0=0, x1=0, equation=255, count=2, parameters=b"bad")),)))
	assert {"ext-4.2#name", "ext-4.2#range", "ext-4.2#equation", "ext-4.2#parameters", "ext-2.2#float"} <= record.reached


def test_exif():
	profile = tiff(entries=((0x0100, 4, 1, 1),))
	check(png(((b"eXIf", profile),)), clean=True)
	check(png(((b"eXIf", b"invalid"),)), warning="11.3.4.5#header")
	check(png(((b"eXIf", profile), (b"eXIf", profile))), warning="5.6#single-ancillary")
	# 5.6 Table 7: eXIf "Before IDAT"; a misplaced chunk is ignored without its content being parsed.
	_, record = check(png(after=((b"eXIf", b"invalid"),)), warning="5.6#before-idat")
	assert "11.3.4.5#header" not in record.reached


def idot_png(*, entries=None, parts=None, body=None, interlace=0, after=False, duplicate=False, bad_crc=False):
	"""Two 1-pixel RGB rows in independent stored DEFLATE blocks, with explicit IDAT starts."""
	raw = b"\0\x12\x34\x56" * 2
	def stored(data, final):
		return bytes((final,)) + struct.pack("<HH", len(data), len(data) ^ 65535) + data
	if parts is None:
		parts = (b"\x78\x01" + stored(raw[:4], 0), stored(raw[4:], 1) + struct.pack(">I", zlib.adler32(raw)))
	if entries is None:
		entries = ((0, 1, 40), (1, 1, 40 + 12 + len(parts[0])))
	if body is None:
		body = struct.pack(">I", len(entries)) + b"".join(struct.pack(">III", *entry) for entry in entries)
	metadata = (corrupt_crc if bad_crc else chunk)(b"iDOT", body)
	if duplicate:
		metadata += metadata
	header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 2, 8, 2, 0, 0, interlace))
	idats = b"".join(chunk(b"IDAT", part) for part in parts)
	return refpng.SIGNATURE + header + (idats + metadata if after else metadata + idats) + chunk(b"IEND", b"")


def idot_cases():
	cases = [("idot#layout", idot_png(body=b"")), ("idot#layout", idot_png(body=struct.pack(">I", 0))),
		("idot#layout", idot_png(body=b"\0\0\0\2" + bytes(20))),
		("idot#single", idot_png(duplicate=True)), ("idot#before-idat", idot_png(after=True)),
		("idot#interlace", idot_png(interlace=1))]
	# Apple's uint32 fields retain their full range; faults belong to iDOT, never the PNG uint31 rule.
	for index, clause in ((0, "idot#rows"), (1, "idot#rows"), (2, "idot#offsets")):
		entry = [0, 1, 40]
		entry[index] = 0xffffffff
		cases.append((clause, idot_png(entries=(tuple(entry), (1, 1, 63)))))
	for entries in (((0, 0, 40), (0, 2, 63)), ((0, 1, 40), (0, 1, 63)), ((0, 1, 40), (2, 1, 63)), ((0, 1, 40), (1, 2, 63))):
		cases.append(("idot#rows", idot_png(entries=entries)))
	for entries in (((0, 1, 41), (1, 1, 63)), ((0, 1, 63), (1, 1, 40)), ((0, 1, 40), (1, 1, 40))):
		cases.append(("idot#offsets", idot_png(entries=entries)))
	# IDAT begins inside a stored block, not at a block boundary.
	stream = zlib.compress(b"\0\x12\x34\x56" * 2, 0)
	cases.append(("idot#independence", idot_png(parts=(stream[:11], stream[11:]))))
	# A real byte-aligned block boundary, but the preceding segment has produced zero rows.
	stream = b"\x78\x01\0\0\0\xff\xff" + zlib.compress(b"\0\x12\x34\x56" * 2, 0)[2:]
	cases.append(("idot#independence", idot_png(parts=(stream[:7], stream[7:]))))
	# Two literal-only fixed blocks share a byte: the second block begins at bit 58, not bit 56 or 64.
	bits = []
	def field(value, width):
		bits.extend(value >> i & 1 for i in range(width))
	def fixed_symbol(symbol):
		code, width = (symbol + 48, 8) if symbol < 144 else (symbol - 256, 7)
		bits.extend(code >> i & 1 for i in reversed(range(width)))
	for final in (0, 1):
		field(final, 1)
		field(1, 2)
		for symbol in b"\0\x12\x34\x56":
			fixed_symbol(symbol)
		fixed_symbol(256)
		if not final:
			assert 16 + len(bits) == 58, "the second block must be unaligned"
	payload = bytes(sum(bit << i for i, bit in enumerate(bits[j:j+8])) for j in range(0, len(bits), 8))
	stream = b"\x78\x01" + payload + struct.pack(">I", zlib.adler32(b"\0\x12\x34\x56" * 2))
	for split in (7, 8):
		cases.append(("idot#independence", idot_png(parts=(stream[:split], stream[split:]))))
	# The second stored block is independent as compressed data; its first scanline needs the previous row.
	for filter_type in (2, 3, 4):
		raw = b"\0\x12\x34\x56" + bytes((filter_type, 0, 0, 0))
		parts = (b"\x78\x01\0\4\0\xfb\xff" + raw[:4], b"\1\4\0\xfb\xff" + raw[4:] + struct.pack(">I", zlib.adler32(raw)))
		cases.append(("idot#filter", idot_png(parts=parts)))
	# A fixed block after Z_SYNC_FLUSH can reference the previous block; the boundary itself is aligned.
	compressor = zlib.compressobj(9)
	first = compressor.compress(b"\0\x12\x34\x56") + compressor.flush(zlib.Z_SYNC_FLUSH)
	second = compressor.compress(b"\0\x12\x34\x56") + compressor.flush(zlib.Z_FINISH)
	cases.append(("idot#independence", idot_png(parts=(first, second))))
	return cases


def without(data, name):
	"""The datastream with every chunk of type `name` removed."""
	pos, out = 8, bytearray(data[:8])
	while pos < len(data):
		end = pos + 12 + int.from_bytes(data[pos:pos + 4], "big")
		if data[pos + 4:pos + 8] != name:
			out += data[pos:end]
		pos = end
	return bytes(out)


def test_idot():
	check(idot_png(), clean=True, pixels=(18, 52, 86, 255) * 2)
	# The length guard rejects large counts before allocating or iterating over entries.
	check(idot_png(body=struct.pack(">I", 0xffffffff)), warning="idot#layout")
	for clause, data in idot_cases():
		new, record = check(data, warning=clause)
		old, _ = check(without(data, b"iDOT"))
		assert tuple(new.samples) == tuple(old.samples), clause
		assert all(CONSEQUENCES[id] == "unaffected" for id, _ in record.warnings), record.warnings
		assert expectations(record) == both("display")
	# A bad iDOT CRC is an ordinary ancillary CRC error: decoders may reject or drop the chunk.
	_, record = check(idot_png(bad_crc=True), warning="13.1#crc-ancillary")
	assert expectations(record) == both("may-reject")
	# A segment-start Sub filter is independent, even when later rows would use previous-row filters.
	raw = b"\0\x12\x34\x56" + b"\1\x12\x34\x56"
	parts = (b"\x78\x01\0\4\0\xfb\xff" + raw[:4], b"\1\4\0\xfb\xff" + raw[4:] + struct.pack(">I", zlib.adler32(raw)))
	check(idot_png(parts=parts), clean=True)
	# Buffered Huffman lookahead must not move the apparent restart point.
	compressor = zlib.compressobj(9)
	first = compressor.compress(raw[:4]) + compressor.flush(zlib.Z_FULL_FLUSH)
	second = compressor.compress(raw[4:]) + compressor.flush(zlib.Z_FINISH)
	check(idot_png(parts=(first, second)), clean=True)
	for path in sorted((ROOT / "decode").glob("y_chunk-idot*.png")):
		data = path.read_bytes()
		new, record = check(data)
		old, _ = check(without(data, b"iDOT"), clean=True)
		assert tuple(new.samples) == tuple(old.samples)
		bad = path.stem.endswith(("-stale", "-short", "-empty"))
		assert bool(record.warnings) == bad, (path, record.warnings)


def test_interpretations():
	"""Every reading the specification leaves open comes from one parse, each under its choices."""
	gray = dict(raw=b"\0\x12", color=0)
	header = 33  # signature and IHDR chunk
	# Several tRNS chunks: the specification asks for recovery and names none to keep.
	data = png(((b"tRNS", b"\0\x12"), (b"tRNS", b"\0\x34")), **gray)
	check(data, warning="5.6#single-ancillary", choices=((("trns", "first"),), (("trns", "last"),)))
	assert interpretations(data) == {(("trns", "first"),): (18, 18, 18, 0), (("trns", "last"),): (18, 18, 18, 255)}
	# A tRNS chunk with a bad CRC is dropped (13.1 recommends it) or used as stored.
	data = png(**gray)
	data = data[:header] + corrupt_crc(b"tRNS", b"\0\x12") + data[header:]
	check(data, warning="13.1#crc-ancillary", choices=((("crc", "dropped"),), (("crc", "ignored"),)))
	assert interpretations(data) == {(("crc", "dropped"),): (18, 18, 18, 255), (("crc", "ignored"),): (18, 18, 18, 0)}
	# Decisions nest rather than multiply: a dropped chunk leaves one tRNS, so no trns choice; nor is it a duplicate.
	data = png(((b"tRNS", b"\0\x34"),), **gray)
	data = data[:header] + corrupt_crc(b"tRNS", b"\0\x12") + data[header:]
	_, record = check(data, choices=((("crc", "dropped"),), (("crc", "ignored"), ("trns", "first")), (("crc", "ignored"), ("trns", "last"))))
	assert "5.6#single-ancillary" not in record.reached
	assert interpretations(data) == {(("crc", "dropped"),): (18, 18, 18, 255), (("crc", "ignored"), ("trns", "first")): (18, 18, 18, 0),
		(("crc", "ignored"), ("trns", "last")): (18, 18, 18, 255)}
	# A tRNS occurrence that is ignored gives no transparency: the first is misplaced here, the last is used.
	data = png(((b"PLTE", b"\x12\x34\x56"), (b"tRNS", b"\0")), raw=b"\0\0", color=3)
	data = data[:header] + chunk(b"tRNS", b"\0") + data[header:]
	check(data, warning="5.6#after-plte", choices=((("trns", "first"),), (("trns", "last"),)), pixels=(18, 52, 86, 255))
	assert interpretations(data)[(("trns", "last"),)] == (18, 52, 86, 0)
	# Two palettes in an indexed-color image; the out-of-range check follows the palette looked up.
	data = png(((b"PLTE", b"\1\2\3"), (b"PLTE", b"\4\5\6\7\x08\x09")), raw=b"\0\1", color=3)
	check(data, warning="11.2.2#single", error="11.2.2#index-in-range", choices=((("plte", "first"),), (("plte", "last"),)))
	assert interpretations(data) == {(("plte", "first"),): (0, 0, 0, 255), (("plte", "last"),): (7, 8, 9, 255)}
	# A palette after IDAT is not one a decoder can keep.
	check(png(((b"PLTE", b"\x12\x34\x56"),), raw=b"\0\0", color=3, after=((b"PLTE", b"\0\0\0"),)),
		warning="11.2.2#single", pixels=(18, 52, 86, 255))
	# An unknown critical chunk after IDAT: decoded as if it were absent.
	check(png(after=((b"CRIT", b"x"),)), error="5.4#unknown-critical", choices=((("critical", "ignored"),),), pixels=(18, 52, 86, 255))
	# A chunk before IHDR takes no part in decoding; an unknown critical one opens the critical choice.
	check(refpng.SIGNATURE + chunk(b"tEXt", b"key\0text") + png()[8:], error="11.2#ihdr-first", pixels=(18, 52, 86, 255))
	check(refpng.SIGNATURE + chunk(b"CgBI", b"\0\0\0\0") + png()[8:], error="11.2#ihdr-first",
		choices=((("critical", "ignored"),),), pixels=(18, 52, 86, 255))
	# A second IHDR is ignored, whatever it says.
	data = png()
	check(data[:header] + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 0, 0, 0, 0)) + data[header:], error="11.2#one-ihdr",
		pixels=(18, 52, 86, 255))
	# IHDR, IDAT and the first indexed PLTE cannot be dropped: a bad CRC leaves only crc:ignored.
	for position in (header - 1, -13):
		data = bytearray(png())
		data[position] ^= 1
		check(bytes(data), warning="13.1#crc-critical", choices=((("crc", "ignored"),),), pixels=(18, 52, 86, 255))
	data = png(((b"PLTE", b"\x12\x34\x56"),), raw=b"\0\0", color=3)
	data = data[:header] + corrupt_crc(b"PLTE", b"\x12\x34\x56") + data[header + 15:]
	check(data, warning="13.1#crc-critical", choices=((("crc", "ignored"),),), pixels=(18, 52, 86, 255))
	# A wrong Adler-32 is recorded at its clause's level, and the complete data are decoded.
	valid = zlib.compress(b"\0\x12\x34\x56")
	_, record = check(png(stream=valid[:-1] + bytes((valid[-1] ^ 1,))), error="rfc1950-2.3#adler32", pixels=(18, 52, 86, 255))
	assert "10.1#deflate-valid" in record.reached
	assert expectations(record) == both("must-reject")
	# A palette index past PLTE is opaque black; both texts' clauses are recorded.
	_, record = check(png(((b"PLTE", b"\x12\x34\x56"),), raw=b"\0\x01", color=3),
		error="11.2.2#index-in-range", warning="13.1#index-opaque-black", pixels=(0, 0, 0, 255))
	# The one contradiction between the texts: an error under the 2nd edition, recovery under the 3rd.
	assert expectations(record) == {"2": "must-reject", "3": "recover"}
	# Truncation: inside the image data no pixels can exist; inside the last CRC the image is complete.
	data = png()
	_, record = check(data[:header + 8 + 3], warning="13.1#end-of-stream", no_image="13.1#data-short")
	assert "10.1#zlib-check" in {id for id, _ in record.warnings}
	check(data[:-12 - 2], warning="13.1#end-of-stream", pixels=(18, 52, 86, 255))
	check(data[:-6], warning="13.1#end-of-stream", pixels=(18, 52, 86, 255))
	# The CLI writes the first interpretation and names every interpretation with its digest, in order.
	data = png(((b"tRNS", b"\0\x12"), (b"tRNS", b"\0\x34")), **gray)
	with tempfile.TemporaryDirectory() as td:
		src, dest = Path(td) / "in.png", Path(td) / "out.pam"
		src.write_bytes(data)
		out = io.StringIO()
		with redirect_stdout(out):
			assert refpng.main([str(src), str(dest)]) == 0
		decoded = refpng.decode(data)
		lines = [l for l in out.getvalue().splitlines() if l.startswith("INFO interpretation=")]
		assert lines == [f"INFO interpretation={c} digest={refpng.digest(i.image)}"
			for c, i in zip(("trns:first", "trns:last"), decoded.interpretations)], lines
		assert dest.read_bytes() == refpng.to_pam(decoded.interpretations[0].image)
		src.write_bytes(png())
		out = io.StringIO()
		with redirect_stdout(out):
			assert refpng.main([str(src), str(dest)]) == 0
		assert "INFO interpretation=none digest=" in out.getvalue()


def test_conformance_mutations():
	"""Each fault regression must fail if its clause's checks are disabled at the checking boundary."""
	_, cases = extension_cases()
	cases += idot_cases()
	cases += [("5.4#reserved-bit", png(((b"vpag", b""),))), ("5.7.2#undefined-public", png(((b"vPAG", b""),)))]
	new_faults = {id for id in CONSEQUENCES if id.startswith(("ext-", "idot#")) and CONSEQUENCES[id] != "none"}
	assert new_faults <= {clause for clause, _ in cases}, new_faults - {clause for clause, _ in cases}
	original_check, original_expect = Parser.check, refpng._State.expect
	for clause, data in cases:
		check(data, warning=clause)
		def muted_check(self, ok, id, message, *, kind):
			return True if id == clause else original_check(self, ok, id, message, kind=kind)
		def muted_expect(self, ok, id, message):
			return True if id == clause else original_expect(self, ok, id, message)
		Parser.check, refpng._State.expect = muted_check, muted_expect
		try:
			try:
				check(data, warning=clause)
			except (AssertionError, IndexError, KeyError) as error:
				# Missing diagnostics fail the regression; missing framing checks can instead hit a parser invariant.
				assert error.args, (clause, "mutation must fail loudly")
			else:
				raise AssertionError((clause, "regression passed with its check removed"))
		finally:
			Parser.check, refpng._State.expect = original_check, original_expect


if __name__ == "__main__":
	main()
