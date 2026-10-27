"""Shrink every test file without changing what it tests.

	uv run minimize.py [test-id ...] [--rounds N]

Each round proposes smaller variants of each file (drop an ancillary chunk, recompress the image
data as one zlib stream, crop the image to half its width or height) and keeps the smallest
variant that passes three checks:
- every implementation's result is unchanged, judged as run.py judges it (all columns rerun): its
  outcome and severity under each lens, its label (the interpretations it matches and the
  properties of the match) and its rating;
- the reference decoder, decoding in-process, records exactly the same fault clause ids, produces
  interpretations with the same choices, and each lens's verdict (conformance, expectation, strict
  notes) is the same;
- any rule the file stops reaching is still reached by another file of the suite.
Files are rewritten in place; afterwards run `uv run expect.py --retrace` and a full run.py.
The memcheck pass is not part of the comparison: compare the full run before and after.
"""
import argparse
import json
import os
import struct
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import expect
import run

ROOT = Path(__file__).resolve().parent
SIG = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def split(data: bytes) -> tuple[list[bytes], bytes] | None:
	"""Raw chunk records (length, type, body and CRC as stored) and trailing bytes; None if not a clean chunk sequence."""
	if not data.startswith(SIG):
		return None
	pos, out = 8, []
	while pos + 12 <= len(data):
		n = struct.unpack(">I", data[pos:pos + 4])[0]
		if pos + 12 + n > len(data):
			return None
		out.append(data[pos:pos + 12 + n])
		pos += 12 + n
		if out[-1][4:8] == b"IEND":
			break
	return out, data[pos:]


def ctype(rec: bytes) -> bytes:
	return rec[4:8]


def body(rec: bytes) -> bytes:
	return rec[8:-4]


def crc_ok(rec: bytes) -> bool:
	return zlib.crc32(rec[4:-4]) & 0xFFFFFFFF == struct.unpack(">I", rec[-4:])[0]


def make(t: bytes, b: bytes) -> bytes:
	return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)


def paeth(a: int, b: int, c: int) -> int:
	p = a + b - c
	pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
	return a if pa <= pb and pa <= pc else b if pb <= pc else c


def unfilter(ft: int, line: bytes, prev: bytes, bpp: int) -> bytes:
	out = bytearray(line)
	for i in range(len(out)):
		a = out[i - bpp] if i >= bpp else 0
		b = prev[i]
		c = prev[i - bpp] if i >= bpp else 0
		out[i] = (out[i] + (0, a, b, (a + b) // 2, paeth(a, b, c))[ft]) & 0xFF
	return bytes(out)


def refilter(ft: int, line: bytes, prev: bytes, bpp: int) -> bytes:
	out = bytearray(len(line))
	for i in range(len(line)):
		a = line[i - bpp] if i >= bpp else 0
		b = prev[i]
		c = prev[i - bpp] if i >= bpp else 0
		out[i] = (line[i] - (0, a, b, (a + b) // 2, paeth(a, b, c))[ft]) & 0xFF
	return bytes(out)


def image_parts(recs: list[bytes]) -> tuple[int, int, int, int, int, int, int, bytes] | None:
	"""(index of first IDAT, number of IDATs, width, height, depth, colour type, interlace, raw scanlines) if clean."""
	idx = [i for i, r in enumerate(recs) if ctype(r) == b"IDAT"]
	if not recs or ctype(recs[0]) != b"IHDR" or len(body(recs[0])) != 13 or not idx or idx != list(range(idx[0], idx[-1] + 1)):
		return None
	if not all(crc_ok(recs[i]) for i in [0] + idx):
		return None
	w, h, depth, ct, _, _, il = struct.unpack(">IIBBBBB", body(recs[0]))
	if ct not in CHANNELS:
		return None
	d = zlib.decompressobj()
	try:
		raw = d.decompress(b"".join(body(recs[i]) for i in idx))
	except zlib.error:
		return None
	if not d.eof or d.unused_data:
		return None
	return idx[0], len(idx), w, h, depth, ct, il, raw


def candidates(data: bytes) -> list[bytes]:
	"""Smaller variants of one file, smallest first."""
	parsed = split(data)
	if parsed is None:
		return []
	recs, tail = parsed
	out = []
	img = image_parts(recs)
	if img:
		first, count, w, h, depth, ct, il, raw = img
		bits = CHANNELS[ct] * depth
		stride = (w * bits + 7) // 8
		bpp = max(1, bits // 8)
		if il == 0 and len(raw) == h * (stride + 1) and all(raw[y * (stride + 1)] <= 4 for y in range(h)):
			rows, prev = [], bytes(stride)
			for y in range(h):
				line = raw[y * (stride + 1):(y + 1) * (stride + 1)]
				prev = unfilter(line[0], line[1:], prev, bpp)
				rows.append((line[0], prev))
			for nw, nh in ((w, max(1, h // 2)), (max(1, w // 2), h) if bits % 8 == 0 else (w, h)):
				if (nw, nh) == (w, h):
					continue
				nstride = (nw * bits + 7) // 8
				new, prev = bytearray(), bytes(nstride)
				for ft, row in rows[:nh]:
					row = row[:nstride]
					new += bytes([ft]) + refilter(ft, row, prev, bpp)
					prev = row
				ihdr = make(b"IHDR", struct.pack(">II", nw, nh) + body(recs[0])[8:])
				idat = make(b"IDAT", zlib.compress(bytes(new), 9))
				out.append(SIG + ihdr + b"".join(recs[1:first]) + idat + b"".join(recs[first + count:]) + tail)
		idat = make(b"IDAT", zlib.compress(raw, 9))
		recompressed = SIG + b"".join(recs[:first]) + idat + b"".join(recs[first + count:]) + tail
		if len(recompressed) < len(data):
			out.append(recompressed)
	frames = [i for i, r in enumerate(recs) if ctype(r) == b"fcTL"]
	actl = [i for i, r in enumerate(recs) if ctype(r) == b"acTL" and len(body(r)) == 8]
	if len(frames) > 2 and len(actl) == 1 and all(crc_ok(recs[i]) for i in frames):
		# keep the first half of the animation; sequence numbers stay contiguous, acTL is told the new count
		cut = frames[len(frames) // 2]
		if all(ctype(r) in (b"fcTL", b"fdAT") for r in recs[cut:-1]) and ctype(recs[-1]) == b"IEND":
			kept = recs[:cut] + recs[-1:]
			n = len(frames) // 2
			a = actl[0]
			kept[a] = make(b"acTL", struct.pack(">I", n) + body(recs[a])[4:])
			out.append(SIG + b"".join(kept) + tail)
	types = {ctype(r) for r in recs[1:] if ctype(r)[0] & 0x20}
	for t in types:
		idx = [i for i, r in enumerate(recs) if ctype(r) == t]
		if len(idx) > 3:  # drop the later half of a repeated chunk type
			drop = set(idx[len(idx) // 2:])
			out.append(SIG + b"".join(r for i, r in enumerate(recs) if i not in drop) + tail)
	for t in sorted(types, key=lambda t: -sum(len(r) for r in recs if ctype(r) == t)):
		if sum(ctype(r) == t for r in recs) > 1:
			out.append(SIG + b"".join(r for r in recs if ctype(r) != t) + tail)
	for i, r in sorted(enumerate(recs), key=lambda e: -len(e[1])):
		if i and ctype(r)[0] & 0x20 and ctype(r) not in (b"IEND",):
			out.append(SIG + b"".join(recs[:i] + recs[i + 1:]) + tail)
	return sorted((c for c in out if len(c) < len(data)), key=len)


def submit(path: Path, impls: list[run.Impl], pool: ProcessPoolExecutor) -> dict:
	return {i.name: pool.submit(run.run_one, i, path, False) for i in impls}


def evaluate(path: Path, futs: dict, impls: list[run.Impl]) -> dict:
	"""Result vector, the reference's judgement and reached rules of one file, judged exactly as run.py judges it."""
	raw = {n: f.result() for n, f in futs.items()}
	reading = expect.reading(path)
	judged = run.judge_test(impls, raw, reading)
	vector = {name: json.dumps({k: j.get(k) for k in ("outcome", "severity", "label", "rating", "strict")}, sort_keys=True)
		for name, j in judged.items()}
	faults = {e.id for e in reading.diagnostics.events if e.level != "INFO"}
	verdicts = {lens: (v.conforms, v.expectation, v.strict_notes) for lens, v in reading.verdicts.items()}
	reference = (faults, tuple(i.choices for i in reading.interpretations), verdicts)
	return {"vector": vector, "reference": reference, "reached": set(reading.diagnostics.reached)}


def main() -> None:
	run.cap_memory()
	ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
	ap.add_argument("tests", nargs="*")
	ap.add_argument("--rounds", type=int, default=40)
	ap.add_argument("--head", type=Path, default=Path.home() / ".cache/difftest-png/head")
	args = ap.parse_args()
	manifest = ROOT / "decode" / "manifest.tsv"
	tests = run.load_manifest(manifest)
	impls = [i for i in run.load_impls(args.head) if run.unavailable(i) is None]
	work = ROOT / "out" / "min"
	work.mkdir(parents=True, exist_ok=True)
	files = {t.id: manifest.parent / t.file for t in tests}
	active = set(args.tests or files)
	with ProcessPoolExecutor(os.cpu_count()) as pool:
		pending = {tid: submit(files[tid], impls, pool) for tid in files}
		base = {tid: evaluate(files[tid], pending[tid], impls) for tid in files}
		before = sum(p.stat().st_size for p in files.values())
		for rnd in range(args.rounds):
			reach_count: dict[str, int] = {}
			for b in base.values():
				for c in b["reached"]:
					reach_count[c] = reach_count.get(c, 0) + 1
			tried = accepted = 0
			proposals = {}
			for tid in sorted(active):
				proposals[tid] = []
				for k, cand in enumerate(candidates(files[tid].read_bytes())[:4]):
					p = work / f"{tid}.{k}.png"
					p.write_bytes(cand)
					proposals[tid].append((cand, p, submit(p, impls, pool)))
			for tid in sorted(active):
				best = None
				for cand, p, futs in proposals[tid]:
					ev = evaluate(p, futs, impls)
					tried += 1
					lost = base[tid]["reached"] - ev["reached"]
					if ev["vector"] == base[tid]["vector"] and ev["reference"] == base[tid]["reference"] and all(reach_count[c] > 1 for c in lost):
						best = (cand, ev)
						break
				if best is None:
					active.discard(tid)
					continue
				files[tid].write_bytes(best[0])
				for c in base[tid]["reached"] - best[1]["reached"]:
					reach_count[c] -= 1
				for c in best[1]["reached"] - base[tid]["reached"]:
					reach_count[c] = reach_count.get(c, 0) + 1
				base[tid] = best[1]
				accepted += 1
			size = sum(p.stat().st_size for p in files.values())
			print(f"[round {rnd + 1}] {tried} candidates, {accepted} files shrunk, {len(active)} still shrinking, {before} -> {size} bytes", file=sys.stderr)
			if not active:
				break


if __name__ == "__main__":
	main()
