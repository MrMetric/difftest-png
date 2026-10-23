"""Plain-assert regressions and read-only corpus comparison.

	uv run python test_refpng.py
	uv run python test_refpng.py --manifest --report /tmp/refpng-comparison.json

Both commands enter a systemd scope with MemoryMax=16G and MemorySwapMax=0 before
running any decodes. --manifest compares EVERY decode/**/*.png in BOTH editions,
using pam.digest's exact canonical representation; it never runs run.py or the pool.
--baseline DIR compares diagnostics/pixels to saved refpng.py + inflate.py (and pngfields.py
when present). A saved pngdiagnostics.py and spec/clauses.tsv isolate the baseline's policies.
With a baseline, stale manifest differences are informational; any baseline
pixel difference still fails the run.
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
import refpng
from pngdiagnostics import CONSEQUENCES, Diagnostics, StreamDiagnostics
from pngfields import (Parser, Values, Ignored, Layout, Struct, UInt, Repeat, Choice,
	Text, Content, Fragment, UINT31, UINT16, BYTE, LANGUAGE_TAG)

ROOT = Path(__file__).resolve().parent


def chunk(name, body):
	return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body))


def png(metadata=(), *, raw=b"\0\x12\x34\x56", stream=None, width=1, height=1, depth=8, colour=2,
		interlace=0, after=()):
	ihdr = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)
	return (refpng.SIGNATURE + chunk(b"IHDR", ihdr) + b"".join(chunk(*c) for c in metadata)
		+ chunk(b"IDAT", zlib.compress(raw) if stream is None else stream)
		+ b"".join(chunk(*c) for c in after) + chunk(b"IEND", b""))


def check(data, edition=3, *, warning=None, reject=None, pixels=None, clean=False):
	record = Diagnostics()
	try:
		image = refpng.decode(data, edition, diagnostics=record)
	except refpng.Reject as e:
		assert e.id == reject, (e.id, reject, str(e))
		assert e.diagnostics is record
		image = None
	else:
		assert reject is None, f"accepted, expected rejection {reject}"
		assert image.diagnostics is record
		if pixels is not None:
			assert tuple(image.samples) == tuple(pixels), (tuple(image.samples), pixels)
	if warning is not None:
		assert warning in {id for id, _ in record.warnings}, (warning, record.warnings)
	if clean:
		assert not record.warnings and image is not None, record.warnings
	assert_reported(record)
	return image, record


def stdout(record):
	out = io.StringIO()
	with redirect_stdout(out):
		record.emit()
	return out.getvalue()


def assert_reported(record):
	# Exercise the actual downstream consumer, without invoking its run functions.
	import expect
	assert not expect.unreported({"stdout": stdout(record)}, CONSEQUENCES)


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
	# Every literal coverage call in the implementation agrees with the catalogue.
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
	d = refpng._State(3, Diagnostics())
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
	for ed in (2, 3):
		check(SINGLETON_PNG, ed, pixels=(127, 127, 127, 255), warning="rfc1951-3.2.2#one-literal-code")
		check(png(stream=SINGLETON_STREAM, colour=0), ed, pixels=(127, 127, 127, 255))


def test_audit_uint31():
	cases = ((b"gAMA", struct.pack(">I", 0x80000000)),
		(b"cHRM", struct.pack(">8I", *([0x80000000] * 8))),
		(b"pHYs", struct.pack(">IIB", 0x80000000, 1, 1)))
	for ed in (2, 3):
		for case in cases:
			check(png((case,)), ed, warning="7.1#uint31", pixels=(18, 52, 86, 255))
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
			d = refpng._State(3, Diagnostics())
			try:
				Parser(d, name, bytes(body), schema, fatal=name == b"IHDR").parse()
			except (Ignored, refpng.Reject):
				assert "11.2.1#dimensions-range" in d.diagnostics.reached or "7.1#uint31" in d.diagnostics.reached
			else:
				raise AssertionError((name, pos, "uint31 escaped validation"))
			assert_reported(d.diagnostics)


def test_audit_srgb():
	bad_chrm = (*refpng.SRGB_CHRM[:-1], 6001)
	for ed in (2, 3):
		for metadata in ((b"gAMA", struct.pack(">I", 100000)), (b"cHRM", struct.pack(">8I", *bad_chrm))):
			for chunks in ((metadata, (b"sRGB", b"\0")), ((b"sRGB", b"\0"), metadata)):
				check(png(chunks), ed, warning="11.3.2.5#compat-values")
		check(png(((b"gAMA", struct.pack(">I", 45455)), (b"cHRM", struct.pack(">8I", *refpng.SRGB_CHRM)),
			(b"sRGB", b"\0"))), ed, clean=True)


def itxt(language, text=b"text", *, compressed=False, translated=b"", keyword=b"Comment", method=0):
	return keyword + b"\0" + bytes((int(compressed), method)) + language + b"\0" + translated + b"\0" + (zlib.compress(text) if compressed else text)


def test_audit_language():
	for language in (b"123", b"en-a", b"x"):
		check(png(((b"iTXt", itxt(language)),)), 3, warning="11.3.3.4#language")
		check(png(((b"iTXt", itxt(language)),)), 2,
			warning="11.3.3.4#language" if language == b"123" else None, clean=language != b"123")
	# Exercise all ABNF branches and the required-content boundary of each branch.
	valid = (b"", b"en", b"zh-cmn-Hans-CN", b"abc-def-ghi-jkl-Latn-123-1996-a-aa-x-z", b"abcd", b"abcdefgh",
		b"de-CH-1901", b"sl-rozaj-biske", b"en-a-foo-b-bar-x-a", b"x-private", b"EN-gb-OED", b"i-klingon", b"sgn-BE-FR")
	invalid = (b"e", b"abcdefghi", b"en-", b"en--US", b"en-a", b"en-x", b"x", b"en-US-Latn", b"en-a-b", b"en_uk", b"en-\xff")
	for language in valid:
		check(png(((b"iTXt", itxt(language)),)), clean=True)
	for language in invalid:
		check(png(((b"iTXt", itxt(language)),)), warning="11.3.3.4#language")


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
	for ed in (2, 3):
		check(data(audit), ed, warning="11.3.2.3#profile")
		check(data(icc()), ed, clean=True)
		check(data(icc((b"text\0\0\0\0example\0",))), ed, clean=True)
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
	check(png(((b"eXIf", b"II*\0"),)), 2, clean=True)
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
	import expect
	raw, stream = long_distance_stream()
	for ed in (2, 3):
		img, record = check(png(stream=stream, width=1023, colour=0), ed, warning="rfc1950-2.2#window-exceeded")
		assert tuple(img.samples[0::4]) == tuple(raw[1:])
		assert expect.outcomes({"verdict": "accept", "stdout": stdout(record)}, CONSEQUENCES) == "pixels=pass,reject=pass"
		if include_fixture:
			fixture = ROOT / "decode/i_zlib-cinfo-0-long-distance.png"
			assert fixture.is_file()
			check(fixture.read_bytes(), ed, warning="rfc1950-2.2#window-exceeded")
	# A subsequent bad Adler cannot erase the recovered window fault.
	bad = stream[:-1] + bytes((stream[-1] ^ 1,))
	check(png(stream=bad, width=1023, colour=0), warning="rfc1950-2.2#window-exceeded", reject="rfc1950-2.3#adler32")


def test_exit_paths():
	# Truncation is recorded before a later invalid filter or short-row rejection can interrupt decoding.
	for raw, fatal in ((b"\x05\x7f", "9.2#filter-types"), (b"\0", "13.1#data-short")):
		_, record = check(png(stream=zlib.compress(raw)[:-4], colour=0), warning="10.1#zlib-check", reject=fatal)
		assert "rfc1950-2.2#end-in-adler32" in record.reached
	# Both the API and CLI preserve accumulated faults on unexpected exceptions, including MemoryError.
	data = png(stream=zlib.compress(b"\0\x7f")[:-4], colour=0)
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
		bad = png(stream=zlib.compress(b"\x05\x7f")[:-4], colour=0)
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
		warning="rfc1950-2.2#window-exceeded", reject="9.2#filter-types")
	assert "rfc1950-2.3#adler32" in record.reached
	assert "11.3.3.3#zlib" in {id for id, _ in record.warnings}
	check(png(((b"iTXt", itxt(b"en", method=17)),)), clean=True)


def test_owner_policies():
	for ed in (2, 3):
		# The sample mask is required under both editions.
		check(png(((b"tRNS", b"\xff\x7f"), (b"bKGD", b"\xff\x7f")), raw=b"\0\x7f", colour=0), ed,
			pixels=(127, 127, 127, 0), clean=True)
		indexed = png(((b"PLTE", b"\x12\x34\x56"),), raw=b"\0\x01", colour=3)
		if ed == 2:
			check(indexed, ed, reject="11.2.2#index-in-range")
		else:
			check(indexed, ed, warning="13.1#index-opaque-black", pixels=(0, 0, 0, 255))
		# CRC failure drops ancillary tRNS; recomputing the CRC uses its pixels instead.
		valid = png(((b"tRNS", b"\0\x12\0\x34\0\x56"),))
		bad = bytearray(valid)
		pos = bad.index(b"tRNS") + 4 + 6
		bad[pos] ^= 1
		check(bytes(bad), ed, warning="13.1#crc-ancillary", pixels=(18, 52, 86, 255))
		check(valid, ed, pixels=(18, 52, 86, 0))
		check(png()[:-12], ed, warning="11.2#iend-present")
		check(png() + b"extra", ed, warning="11.2#iend-last")
		check(png(raw=b"\0\x12\x34\x56extra"), ed, warning="13.1#data-long")
		check(png(raw=b"\0\x12"), ed, reject="13.1#data-short")
		check(png(((b"acTL", b"\0" * 8),)), ed,
			warning="11.3.6.1#num-frames-zero" if ed == 3 else None, pixels=(18, 52, 86, 255))


def test_small_pixels():
	# Independent sample packing across every legal colour/depth combination, and all five inverse filters.
	for colour, depths in ((0, (1, 2, 4, 8, 16)), (2, (8, 16)), (3, (1, 2, 4, 8)), (4, (8, 16)), (6, (8, 16))):
		for depth in depths:
			nch = refpng.CHANNELS[colour]
			maxval = (1 << depth) - 1
			width, height = 3, 2
			rng = random.Random(colour * 100 + depth)
			rows = [[rng.randrange(maxval + 1) for _ in range(width * nch)] for _ in range(height)]
			if colour == 3:
				rows = [[v % min(4, maxval + 1) for v in row] for row in rows]
			palette = bytes((13, 27, 53, 81, 127, 195, 0, 255, 99, 255, 11, 7))[:3 * min(4, maxval + 1)]
			metadata = ((b"PLTE", palette),) if colour == 3 else ()
			pixels = []
			for row in rows:
				for x in range(width):
					v = row[x * nch:(x + 1) * nch]
					outmax = 65535 if depth == 16 else 255
					if colour == 3:
						pixels.extend((*palette[3 * v[0]:3 * v[0] + 3], 255))
					elif colour in (0, 4):
						g = v[0] * (outmax // maxval)
						pixels.extend((g, g, g, v[1] if colour == 4 else outmax))
					else:
						pixels.extend((*v[:3], v[3] if colour == 6 else outmax))
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
					for ed in (2, 3):
						check(png(metadata, raw=bytes(raw), width=width, height=height, colour=colour, depth=depth, interlace=interlace),
							ed, pixels=pixels, clean=True)


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
	"""Isolate the baseline modules, using its own recorder when its catalogue is supplied."""
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
	import csv
	from collections import Counter
	import expect
	import pam
	with (ROOT / "decode/manifest.tsv").open(newline="") as f:
		rows = {r["file"]: r for r in csv.DictReader(f, delimiter="\t")}
	files = sorted((ROOT / "decode").rglob("*.png"))
	assert {str(p.relative_to(ROOT / "decode")) for p in files} == set(rows), "manifest and PNG file sets differ"
	result = {"files": len(files), "edition_runs": 0, "hash_differences": [], "outcome_differences": [],
		"baseline_pixel_differences": [], "baseline_diagnostic_differences": []}
	def run_one(module, data, ed):
		try:
			img = module.decode(data, ed)
		except module.Reject as e:
			warnings, reached, verdict, digest, fatal = e.warnings, e.reached, "reject", "", e.id
			fatal_message = str(e)
		else:
			warnings, reached, verdict, fatal = img.warnings, img.reached, "accept", None
			digest = pam.digest(pam.rgba16(pam.parse(module.to_pam(img))))
		lines = ["INFO reached=" + ",".join(sorted(reached)), *(f"WARN [{id}] {msg}" for id, msg in warnings)]
		if fatal:
			lines.append(f"ERR [{fatal}] {fatal_message}")
		raw = {"stdout": "\n".join(lines), "verdict": verdict, "detail": ""}
		silent = sorted(expect.unreported(raw, CONSEQUENCES))
		if module is refpng:
			assert not silent, (module.__name__, ed, silent)
		return {"diagnostics": lines[1:], "reached": sorted(reached), "unreported": silent, "hash": digest, "outcome": expect.outcomes(raw, CONSEQUENCES), "faults": sorted(set(id for id, _ in warnings) | ({fatal} if fatal else set()))}
	for index, file in enumerate(files):
		name = str(file.relative_to(ROOT / "decode"))
		data, row = file.read_bytes(), rows[name]
		for ed in (2, 3):
			actual = run_one(refpng, data, ed)
			result["edition_runs"] += 1
			where = {"file": name, "edition": ed}
			if actual["hash"] not in row[f"hash{ed}"].split("|"):
				result["hash_differences"].append(where | {"expected": row[f"hash{ed}"], "actual": actual})
			if actual["outcome"] != row[f"ed{ed}"]:
				result["outcome_differences"].append(where | {"expected": row[f"ed{ed}"], "actual": actual})
			if baseline:
				old = run_one(baseline, data, ed)
				if old["hash"] != actual["hash"]:
					result["baseline_pixel_differences"].append(where | {"before": old, "after": actual})
				if (old["diagnostics"], old["reached"], old["outcome"], old["unreported"]) != (actual["diagnostics"], actual["reached"], actual["outcome"], actual["unreported"]):
					result["baseline_diagnostic_differences"].append(where | {"before": old, "after": actual,
						"added": list((Counter(actual["diagnostics"]) - Counter(old["diagnostics"])).elements()),
						"removed": list((Counter(old["diagnostics"]) - Counter(actual["diagnostics"])).elements())})
		if index % 25 == 0:
			print(f"Compared {index + 1}/{len(files)} files, both editions", flush=True)
		if report_path:
			report_path.write_text(json.dumps(result, indent=2) + "\n")
	print(json.dumps(result, indent=2))
	if baseline is None:
		assert not result["hash_differences"], "manifest pixel differences require review; see report"
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
		test_hdist_policy, test_corrupt_image_tail, test_structure_before_crc, test_diagnostic_severity):
		test()
		print(test.__name__ + ": passed", flush=True)
	if args.manifest:
		compare_manifest(args.report, load_baseline(args.baseline) if args.baseline else None)
	print("All requested checks passed")


def test_independent_chunk_faults():
	import expect
	_, stream = long_distance_stream()
	for ed in (2, 3):
		_, record = check(png(((b"zTXt", b"Two  spaces\0\0" + stream),)), ed)
		ids = {id for id, _ in record.warnings}
		assert {"11.3.3.1#keyword-spaces", "rfc1950-2.2#window-exceeded"} <= ids, ids
		assert expect.outcomes({"verdict": "accept", "stdout": stdout(record)}, CONSEQUENCES) == "pixels=pass,reject=pass"


def test_value_then_layout_fault():
	# r_ztxt-len-1's complete zTXt body: empty keyword and null, with no method byte.
	for ed in (2, 3):
		_, record = check(png(((b"zTXt", bytes.fromhex("00")),)), ed)
		ids = {id for id, _ in record.warnings}
		assert ids == {"11.3.3.1#keyword-length", "11.3.3.3#separator"}, ids


def parse_faults(schema, data, *, state=None):
	d = state if state is not None else refpng._State(3, Diagnostics())
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
	assert {id for id, _ in record.warnings} == {"11.3.3.4#flag", "11.3.3.4#language", "11.3.3.4#utf8"}
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
			{"11.3.3.4#method", "11.3.3.4#language", "11.3.3.4#utf8"}))
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
	d = refpng._State(3, Diagnostics())
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
	for ed in (2, 3):
		_, record = check(png(width=0x80000000, height=0x80000001), ed, reject="11.2.1#dimensions-range")
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
	import expect
	for ed in (2, 3):
		_, record = check(HDIST31_PNG, ed, clean=True)
		assert "rfc1951-3.2.7#hdist-over-30" in record.reached
		assert expect.outcomes({"verdict": "accept", "stdout": stdout(record)}, CONSEQUENCES) == "pixels=pass"


def test_corrupt_image_tail():
	import expect
	for ed in (2, 3):
		_, record = check(CORRUPT_TAIL_PNG, ed, pixels=(18, 52, 86, 255))
		assert "rfc1951-3.2.3#distance-too-far" in record.reached
		assert expect.outcomes({"verdict": "accept", "stdout": stdout(record)}, CONSEQUENCES) == "pixels=pass,reject=pass"
	# A bad block header, including one whose table construction fails, has the same completion policy.
	def packed(fields):
		bits = [value >> i & 1 for value, width in fields for i in range(width)]
		return bytes(sum(bit << i for i, bit in enumerate(bits[j:j+8])) for j in range(0, len(bits), 8))
	oversubscribed = packed(((1, 1), (2, 2), (0, 5), (0, 5), (0, 4), *((1, 3),) * 4))
	def stream(raw, tail):
		return b"\x78\x01\0" + struct.pack("<HH", len(raw), len(raw) ^ 65535) + raw + tail
	for ed in (2, 3):
		for tail, cause in ((b"\x07", "rfc1951-3.2.3#btype-reserved"),
			(oversubscribed, "rfc1951-3.2.2#over-subscribed")):
			for raw in (b"\0\x12\x34\x56", b"\0\x12\x34\x56extra"):
				_, record = check(png(stream=stream(raw, tail)), ed,
					warning="10.1#zlib-check", pixels=(18, 52, 86, 255))
				assert any(e.id == "10.1#zlib-check" and e.cause == cause for e in record.events)
				assert not any(e.level == "ERR" for e in record.events)
				assert expect.outcomes({"verdict": "accept", "stdout": stdout(record)}, CONSEQUENCES) == "pixels=pass,reject=pass"
			check(png(stream=stream(b"\0\x12\x34", tail)), ed, reject=cause)
		# A 2x2 one-bit Adam7 image has three nonempty passes and six bytes, including their filters.
		check(png(stream=stream(b"\0\0\0\0\0\0", b"\x07"), colour=0, depth=1, width=2, height=2, interlace=1),
			ed, warning="10.1#zlib-check", pixels=(0, 0, 0, 255) * 4)
		check(png(stream=stream(b"\0\0\0\0\0", b"\x07"), colour=0, depth=1, width=2, height=2, interlace=1),
			ed, reject="rfc1951-3.2.3#btype-reserved")
		valid = zlib.compress(b"\0\x12\x34\x56")
		check(png(stream=valid[:-1] + bytes((valid[-1] ^ 1,))), ed, reject="rfc1950-2.3#adler32")


def corrupt_crc(name, body):
	data = bytearray(chunk(name, body))
	data[-1] ^= 1
	return bytes(data)


def test_structure_before_crc():
	import expect
	header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
	idat = chunk(b"IDAT", zlib.compress(b"\0\x12\x34\x56"))
	indexed = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
	for ed in (2, 3):
		for data, id in (
			(refpng.SIGNATURE + header + corrupt_crc(b"IHDR", header[8:-4]), "11.2#one-ihdr"),
			(refpng.SIGNATURE + header + corrupt_crc(b"ABCD", b""), "5.4#unknown-critical"),
			(refpng.SIGNATURE + corrupt_crc(b"IDAT", b""), "11.2#ihdr-first"),
			(refpng.SIGNATURE + header + idat + chunk(b"tEXt", b"key\0text") + corrupt_crc(b"IDAT", b""), "11.2.3#consecutive"),
			(refpng.SIGNATURE + header + corrupt_crc(b"IEND", b""), "11.2#idat-present"),
			(refpng.SIGNATURE + header + corrupt_crc(b"A\xffCD", b""), "5.4#unknown-critical"),
			(refpng.SIGNATURE + corrupt_crc(b"a\xffCD", b""), "11.2#ihdr-first"),
			(refpng.SIGNATURE + corrupt_crc(b"\nfoo", b""), "11.2#ihdr-first"),
			(refpng.SIGNATURE + corrupt_crc(b"IHDR", b""), "11.2.1#length"),
			(refpng.SIGNATURE + indexed + corrupt_crc(b"PLTE", b"\0"), "11.2.2#invalid-indexed"),
			(refpng.SIGNATURE + indexed + corrupt_crc(b"PLTE", b""), "11.2.2#invalid-indexed"),
			(refpng.SIGNATURE + indexed + corrupt_crc(b"IDAT", b""), "11.2.2#required")):
			_, record = check(data, ed, reject=id)
			assert "13.1#crc-critical" not in record.reached and "5.3#type-letters" not in record.reached
			assert expect.outcomes({"verdict": "reject", "stdout": stdout(record), "detail": ""}, CONSEQUENCES) == "reject=pass"


def test_diagnostic_severity():
	allowed = {"INFO": {"none"}, "WARN": {"recover", "either", "still"}, "ERR": {"reject", "either"}}
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


if __name__ == "__main__":
	main()
