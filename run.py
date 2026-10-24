"""Run the PNG test suite against every implementation available here and write the report.

	uv run run.py [impl ...] [--head DIR] [--jobs N] [--no-memcheck] [--no-build] [--out DIR]
				  [--reuse [--rerun IMPL ...]]

Implementations come from impls.toml; a column whose program is missing is skipped with a
notice. Each (implementation, test) runs under address-space, file-size and wall-clock limits;
native columns also run under valgrind memcheck. Every run is fresh unless --reuse is given: then
results stored by earlier runs are used for every column except the reference decoder and those
named in --rerun (name a column there after rebuilding or upgrading it).
The whole run sits in a systemd scope capped at half the machine's memory. The outcome is judged against
decode/manifest.tsv for each edition, and out/report.json plus out/report.html are written.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import zlib
from compression import zstd
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import execute
from execute import child_env, run_one

ROOT = Path(__file__).resolve().parent
EDITIONS = (2, 3)
SEVERITY = {"pass": 0, "should": 1, "must": 2}
FATAL = ("crash", "timeout", "oom", "memory", "shim")  # always a must-violation
# Not a performance limit: no PNG decoder should need this long, and slow decodes that do finish keep their
# real outcome instead of becoming a timeout when the machine is busy.
TIMEOUT_S = 60.0


@dataclass
class Impl:
	name: str
	project: str
	variant: str
	lineage: str
	cmd: list[str]
	notes: str = ""
	pixels: bool = True
	version_cmd: list[str] | None = None
	revision: str | None = None
	build: list[str] | None = None
	creates: str | None = None  # the file whose existence means the build step has run
	mem_mb: int = 2048
	timeout_s: float = TIMEOUT_S
	max_depth: int = 16
	env: dict[str, str] = field(default_factory=dict)
	memcheck: bool = False


@dataclass
class Test:
	id: str
	file: str
	clause: str
	reaches: list[str]
	allowed: dict[int, dict[str, str]]  # edition -> outcome -> severity
	hashes: dict[int, list[str]]  # edition -> acceptable canonical RGBA16 digests
	conforms: dict[int, bool]  # edition -> whether the datastream conforms (what validators are judged on)
	description: str


def load_impls(head: Path) -> list[Impl]:
	subst = {"{python}": sys.executable, "{head}": str(head)}
	def expand(s: str) -> str:
		for k, v in subst.items():
			s = s.replace(k, v)
		if not s.startswith(("/", "-")) and "{" not in s and ("/" in s and (ROOT / s.split("/")[0]).is_dir() or (ROOT / s).is_file()):
			s = str(ROOT / s)
		return s
	with open(ROOT / "impls.toml", "rb") as f:
		raw = tomllib.load(f)
	known = {"project", "variant", "lineage", "cmd", "notes", "pixels", "version", "revision", "build", "creates", "mem_mb",
		"timeout_s", "max_depth", "env", "memcheck"}
	out = []
	for name, d in raw.items():
		assert d.keys() <= known, f"{name}: unknown keys {sorted(d.keys() - known)}"
		assert d.get("pixels", True) is False or "{output}" in d["cmd"] or any("{output}" in a for a in d["cmd"]), name
		out.append(Impl(
			name=name, project=d["project"], variant=d["variant"], lineage=d["lineage"],
			cmd=[expand(a) for a in d["cmd"]], notes=d.get("notes", ""), pixels=d.get("pixels", True),
			version_cmd=[expand(a) for a in d["version"]] if "version" in d else None,
			revision=d.get("revision"), build=[expand(a) for a in d["build"]] if "build" in d else None,
			creates=expand(d["creates"]) if "creates" in d else None,
			mem_mb=d.get("mem_mb", 2048), timeout_s=d.get("timeout_s", TIMEOUT_S), max_depth=d.get("max_depth", 16),
			env={k: expand(v) for k, v in d.get("env", {}).items()},
			memcheck=d.get("memcheck", False),
		))
	return out


def parse_outcomes(spec: str) -> dict[str, str]:
	"""`pixels=pass,reject=should` -> {"pixels": "pass", "reject": "should"}."""
	out = {}
	for item in spec.split(","):
		k, _, v = item.partition("=")
		assert k in ("pixels", "reject", "accept") and v in SEVERITY, f"bad outcome item {item!r}"
		out[k] = v
	return out


def load_manifest(path: Path) -> list[Test]:
	tests = []
	with open(path, newline="") as f:
		for r in csv.DictReader(f, delimiter="\t"):
			tests.append(Test(
				id=r["id"], file=r["file"], clause=r["clause"],
				reaches=[c for c in r["reaches"].split(",") if c],
				allowed={2: parse_outcomes(r["ed2"]), 3: parse_outcomes(r["ed3"])},
				hashes={2: [h for h in r["hash2"].split("|") if h], 3: [h for h in r["hash3"].split("|") if h]},
				conforms={ed: {"yes": True, "no": False}[r[f"conforms{ed}"]] for ed in EDITIONS},
				description=r["description"],
			))
	assert len({t.id for t in tests}) == len(tests), "duplicate test ids"
	return tests


def unavailable(impl: Impl) -> str | None:
	prog = impl.cmd[0]
	if "/" in prog:
		if not Path(prog).exists():
			return f"{prog} not built"
	elif shutil.which(prog) is None:
		return f"{prog} not installed"
	return None


def version_of(impl: Impl) -> str | None:
	if not impl.version_cmd:
		return None
	try:
		r = subprocess.run(impl.version_cmd, cwd=ROOT, capture_output=True, text=True, timeout=60,
			env=child_env(impl))
	except (OSError, subprocess.SubprocessError) as e:
		return f"version unknown ({type(e).__name__})"
	lines = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
	if not lines:
		return None
	v = lines[0].split("Version: ")[-1]
	for cut in (" Copyright", " http", " (c)"):
		v = v.split(cut)[0]
	v = v.rstrip(", ")
	if "#" in v and v.startswith(("registry+", "path+", "git+")):  # cargo pkgid: keep "name version"
		v = v.rsplit("#", 1)[1].replace("@", " ")
	return v


_unpacked: dict[str, np.ndarray] = {}


def pixels(image: tuple[str, tuple, bytes]) -> np.ndarray:
	"""The pixels of a packed image; the last few are kept, so judging one test decompresses each output once."""
	digest, shape, z = image
	if digest not in _unpacked:
		if len(_unpacked) >= 8:
			_unpacked.clear()
		_unpacked[digest] = np.frombuffer(zstd.decompress(z), dtype=np.uint16).reshape(shape)
	return _unpacked[digest]


_hidden: dict[str, bool] = {}


def has_hidden_colour(image: tuple[str, tuple, bytes]) -> bool:
	"""Does the image hold a fully transparent pixel whose colour is not black?"""
	if image[0] not in _hidden:
		a = pixels(image)
		_hidden[image[0]] = bool(np.any((a[:, :, 3] == 0) & np.any(a[:, :, :3] != 0, axis=2)))
	return _hidden[image[0]]


def reached_of(stdout: str) -> set[str]:
	"""Clause ids the reference decoder reports reaching: its `INFO reached=` line plus every WARN/ERR id."""
	out = set()
	for line in stdout.splitlines():
		if line.startswith("INFO reached="):
			out |= {c for c in line[len("INFO reached="):].split(",") if c}
		elif line.startswith("INFO features="):  # the stopgap reference's form
			out |= {c for c in line[len("INFO features="):].split(";") if c}
		elif line.startswith(("WARN [", "ERR [")):
			out.add(line.split("[", 1)[1].split("]", 1)[0])
	return out


def compare(impl: Impl, got: np.ndarray, maxval: int, want: np.ndarray) -> tuple[bool, dict]:
	"""Do the decoded pixels match the expected ones? Returns (match, properties of the match)."""
	props = {}
	if got.shape != want.shape:
		return False, props
	if impl.max_depth == 8 and maxval <= 255 and want.max(initial=0) > 0 and np.any(want % 257):
		high = (want >> 8) * 257
		rounded = ((want.astype(np.uint32) * 255 + 32767) // 65535 * 257).astype(np.uint16)
		for name, cand in (("high byte", high), ("rounded", rounded)):
			if visible_equal(got, cand):
				props["16to8"] = name
				want = cand
				break
		else:
			return False, props
	elif not visible_equal(got, want):
		return False, props
	hidden = (want[:, :, 3] == 0) & np.any(want[:, :, :3] != 0, axis=2)
	if hidden.any():
		if np.array_equal(got[hidden], want[hidden]):
			props["alpha0"] = "preserved"
		elif not np.any(got[hidden][:, :3]):
			props["alpha0"] = "zeroed"
		else:
			props["alpha0"] = "altered"
	return True, props


def visible_equal(a: np.ndarray, b: np.ndarray) -> bool:
	if a.shape != b.shape or not np.array_equal(a[:, :, 3], b[:, :, 3]):
		return False
	shown = a[:, :, 3] != 0
	return np.array_equal(a[shown], b[shown])


def validator_allowed(conforms: bool) -> dict[str, str]:
	"""A validator is judged on whether the datastream conforms, not on what a decoder must do with it:
	it must accept exactly the conforming files and flag every other one."""
	return {"pixels": "pass"} if conforms else {"reject": "pass"}


def _chunks(data: bytes) -> tuple[list[bytes], bytes] | None:
	"""Raw chunk records (length, type, data and CRC as stored) and what follows IEND; None if they do not parse."""
	if not data.startswith(b"\x89PNG\r\n\x1a\n"):
		return None
	pos, out = 8, []
	while pos + 12 <= len(data):
		n = int.from_bytes(data[pos:pos + 4], "big")
		if pos + 12 + n > len(data):
			return None
		out.append(data[pos:pos + 12 + n])
		pos += 12 + n
		if out[-1][4:8] == b"IEND":
			break
	return out, data[pos:]


def alternatives(data: bytes, reference_stdout: str) -> dict[str, bytes]:
	"""Other datastreams whose reference pixels are as right as the file's own, by the label the report shows.

	crc-repaired: checking CRCs is the decoder's choice (13.1 "may be regarded as a fatal error"), so the image a
	decoder gets by using an ancillary chunk with a bad CRC as stored is as right as the one with the chunk dropped.
	last-tRNS: with more than one tRNS chunk the specification asks for recovery without saying which chunk to keep;
	keeping the last, as if the earlier ones did not exist, is as coherent as the reference's keeping the first.
	"""
	parsed = _chunks(data)
	if parsed is None:
		return {}
	recs, tail = parsed
	out = {}
	if "WARN [13.1#crc-ancillary]" in reference_stdout:
		out["crc-repaired"] = data[:8] + b"".join(r[:-4] + zlib.crc32(r[4:-4]).to_bytes(4, "big") for r in recs) + tail
	trns = [i for i, r in enumerate(recs) if r[4:8] == b"tRNS"]
	if len(trns) > 1:
		out["last-tRNS"] = data[:8] + b"".join(r for i, r in enumerate(recs) if i not in trns[:-1]) + tail
	return out


def reference_alternatives(refs: dict[int, Impl], file: Path, reference_stdout: str) -> dict[int, list[tuple[str, tuple]]]:
	"""Per edition, (label, reference pixels) of each of the file's alternatives that the reference decodes."""
	out: dict[int, list[tuple[str, tuple]]] = {ed: [] for ed in EDITIONS}
	work = ROOT / "out" / "alternatives"
	work.mkdir(parents=True, exist_ok=True)
	for label, data in alternatives(file.read_bytes(), reference_stdout).items():
		alt = work / f"{file.stem}.{label}.png"
		alt.write_bytes(data)
		for ed in EDITIONS:
			r = run_one(refs[ed], alt, False)
			if r["verdict"] == "accept":
				out[ed].append((label, r["image"]))
	return out


def judge(test: Test, impl: Impl, raw: dict, expected: dict[int, tuple | None], alts: dict[int, list[tuple[str, tuple]]] | None = None) -> dict:
	"""Outcome of one run and its severity under each edition. Pixels matching one of the test's alternatives
	pass like the expected ones, and the result records which alternative they matched."""
	v = raw["verdict"]
	res = {"detail": raw["detail"]}
	if raw.get("memory"):
		res["memory"] = raw["memory"]  # in addition to the outcome of the normal run, which it does not hide
	if raw.get("memcheck_timeout") and v not in FATAL:
		v = "timeout"
		res["detail"] = f"killed after {execute.MEMCHECK_TIMEOUT_S:g} s under valgrind"
	sev = {}
	for ed in EDITIONS:
		outcome = v
		if v == "accept":
			want = expected[ed]
			if not impl.pixels:
				outcome = "pixels"
			elif want is None:
				outcome = "accept"  # decoded what the reference rejects
			else:
				if raw["image"][0] == want[0]:  # identical pixels: no array work beyond the alpha-0 question
					ok, props = True, ({"alpha0": "preserved"} if has_hidden_colour(want) else {})
				else:
					ok, props = compare(impl, pixels(raw["image"]), raw["maxval"], pixels(want))
				for label, alt in (alts or {}).get(ed, []) if not ok else []:
					ok, props = compare(impl, pixels(raw["image"]), raw["maxval"], pixels(alt))
					if ok:
						props["alternative"] = label
						break
				outcome = "pixels" if ok else "wrong"
				res.update(props)
		res.setdefault("outcome", {})[ed] = outcome
		allowed = test.allowed[ed] if impl.pixels else validator_allowed(test.conforms[ed])
		if outcome in FATAL or outcome not in allowed or raw.get("memory"):
			sev[ed] = "must"
		else:
			sev[ed] = allowed[outcome]
	res["severity"] = sev
	res["editions"] = [ed for ed in EDITIONS if sev[ed] == "pass"]
	outcomes = set(res["outcome"].values())
	res["outcome"] = outcomes.pop() if len(outcomes) == 1 else res["outcome"]
	res["worst"] = min(sev.values(), key=lambda x: SEVERITY[x])  # a test fails only if it fails under every edition
	if raw["warning"] and v == "accept":
		res["warning"] = raw["warning"]
	return res


def discriminating(t: Test, impl: Impl) -> bool:
	"""The editions contradict each other on this file, in what the implementation is judged on.

	For a decoder: they expect different pixels, or one expects none. A difference in allowed outcomes
	alone is not a contradiction: the chunks the third edition adds (APNG, cICP) are unknown ancillary
	chunks to the second, which changes what a fault in them means but not what image the file holds.
	For a validator: one edition's datastream conforms and the other's doesn't.
	"""
	return pixels_differ(t) if impl.pixels else t.conforms[2] != t.conforms[3]


def pixels_differ(t: Test) -> bool:
	return t.hashes[2][:1] != t.hashes[3][:1]


def summarize(impl: Impl, results: dict[str, dict], tests: list[Test]) -> dict:
	disc = {t.id for t in tests if discriminating(t, impl)}
	s: dict = {"must": 0, "should": 0, "pass": 0}
	for r in results.values():
		s[r["worst"]] += 1
	only = {ed: [t for t in disc if results[t]["editions"] == [ed]] for ed in EDITIONS}
	# The edition an implementation follows where the editions contradict each other. A test it fails
	# under both editions is a bug, not evidence of an edition; "mixed" is following each on different tests.
	consistent = [ed for ed in EDITIONS if all(ed in results[t]["editions"] for t in disc)]
	if consistent:
		s["edition"] = "/".join(f"{ed}" for ed in consistent)
	elif only[2] and only[3]:
		s["edition"] = "mixed"
	elif only[2] or only[3]:
		s["edition"] = "2" if only[2] else "3"
	else:
		s["edition"] = "neither"
	s["edition_tests"] = {str(ed): sorted(only[ed]) for ed in EDITIONS}
	for prop in ("alpha0", "16to8"):
		seen = sorted({r[prop] for r in results.values() if prop in r})
		if seen:
			s[prop] = seen
	return s


def built(impl: Impl) -> bool:
	target = impl.creates or (impl.cmd[0] if "/" in impl.cmd[0] else None)
	assert target, f"{impl.name}: a build step needs `creates` when the program is not a path"
	return Path(target).exists()


def build(impls: list[Impl], force: set[str]) -> None:
	"""Run each build step whose output is missing, or that is named in `force`."""
	done = set()
	for impl in impls:
		if not impl.build or (built(impl) and not force & {impl.name, impl.project, "all"}):
			continue
		if tuple(impl.build) not in done:
			done.add(tuple(impl.build))
			print(f"[build] {impl.name}: {' '.join(impl.build)}", file=sys.stderr)
			try:
				r = subprocess.run(impl.build, cwd=ROOT, env=os.environ | impl.env)  # builds use the caller's toolchains
				if r.returncode:
					print(f"[build] {impl.name}: failed (exit {r.returncode})", file=sys.stderr)
			except OSError as e:
				print(f"[build] {impl.name}: {e}", file=sys.stderr)


def cap_memory(fraction: float = 0.5) -> None:
	"""Re-execute this command inside a systemd scope whose memory is capped, unless it already is.

	Each decoder already runs under RLIMIT_AS; this bounds the harness itself and everything it starts
	together, so a runaway job is killed alone instead of the kernel's OOM killer choosing a victim.
	"""
	if os.environ.get("DIFFTEST_MEMORY_CAP") or shutil.which("systemd-run") is None:
		if not os.environ.get("DIFFTEST_MEMORY_CAP"):
			print("[memory] systemd-run not found; the harness itself runs without a memory cap", file=sys.stderr)
		return
	cap = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") * fraction)
	os.environ["DIFFTEST_MEMORY_CAP"] = str(cap)
	argv = ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={cap}", "-p", "MemorySwapMax=0",
		sys.executable, *sys.argv]
	os.execvp(argv[0], argv)


def main() -> None:
	cap_memory()
	ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
	ap.add_argument("impls", nargs="*", help="implementation names or projects (default: every available one)")
	ap.add_argument("--head", type=Path, default=Path(os.environ.get("DIFFTEST_HEAD", Path.home() / ".cache/difftest-png/head")),
		help="directory of upstream HEAD builds")
	ap.add_argument("--manifest", type=Path, default=ROOT / "decode" / "manifest.tsv")
	ap.add_argument("--out", type=Path, default=ROOT / "out")
	ap.add_argument("--jobs", type=int, default=os.cpu_count())
	ap.add_argument("--no-memcheck", action="store_true")
	ap.add_argument("--no-build", action="store_true")
	ap.add_argument("--rebuild", nargs="*", default=[], metavar="IMPL", help="rerun build steps even if built (no names: all)")
	ap.add_argument("--no-trace", action="store_true", help="skip the coverage-trace check")
	ap.add_argument("--reuse", action="store_true", help="use stored results of earlier runs (never for the reference decoder)")
	ap.add_argument("--rerun", nargs="+", default=[], metavar="IMPL", help="with --reuse, run these implementations or projects afresh")
	args = ap.parse_args()
	assert args.reuse or not args.rerun, "--rerun only means something with --reuse"

	tests = load_manifest(args.manifest)
	assert tests, f"{args.manifest} holds no tests"
	impls = load_impls(args.head)
	refs = {ed: f"reference-ed{ed}" for ed in EDITIONS}
	if args.impls:
		wanted = set(args.impls)
		impls = [i for i in impls if i.name in wanted or i.project in wanted or i.name in refs.values()]
	if not args.no_build:
		build(impls, set(args.rebuild) or ({"all"} if args.rebuild == [] and "--rebuild" in sys.argv else set()))
	avail = []
	for impl in impls:
		why = unavailable(impl)
		if why:
			print(f"[skip] {impl.name}: {why}", file=sys.stderr)
		else:
			avail.append(impl)
	assert all(any(i.name == r for i in avail) for r in refs.values()), "the reference decoder must be runnable"
	memcheck = not args.no_memcheck and shutil.which("valgrind") is not None
	if not args.no_memcheck and not memcheck:
		print("[memcheck] valgrind not found; memory pass skipped", file=sys.stderr)

	t0 = time.monotonic()
	fresh = set(args.rerun)
	reuse = {i.name for i in avail if args.reuse and not fresh & {i.name, i.project}}
	raw, missing = run_all(avail, tests, args.manifest.parent, memcheck, args.jobs, reuse)
	for name, why in sorted(missing.items()):
		print(f"[skip] {name}: not installed ({why})", file=sys.stderr)
	avail = [i for i in avail if i.name not in missing]
	assert all(any(i.name == r for i in avail) for r in refs.values()), "the reference decoder must be runnable"
	print(f"[run] {len(raw)} results in {time.monotonic() - t0:.0f} s", file=sys.stderr)
	_judge_and_report(args, tests, avail, refs, raw, memcheck)


def run_all(avail: list[Impl], tests: list[Test], base: Path, memcheck: bool, jobs: int, reuse: set[str] = frozenset()
		) -> tuple[dict[tuple[str, str], dict], dict[str, str]]:
	"""(raw result per (implementation, test), why each implementation found not installed is missing)."""
	raw: dict[tuple[str, str], dict] = {}
	images: dict[str, tuple] = {}
	missing: dict[str, str] = {}
	with ProcessPoolExecutor(max_workers=jobs) as ex:
		futs = {ex.submit(run_one, impl, base / t.file, False, impl.name in reuse): (impl.name, t.id) for impl in avail for t in tests}
		if memcheck:
			futs |= {ex.submit(run_one, impl, base / t.file, True, impl.name in reuse): (impl.name, t.id, "mem")
				for impl in avail if impl.memcheck for t in tests}
		for n, fut in enumerate(as_completed(futs), 1):
			key = futs[fut]
			try:
				r = fut.result()
			except execute.Missing as e:
				missing.setdefault(key[0], str(e))
				continue
			if r.get("image"):
				r["image"] = images.setdefault(r["image"][0], r["image"])  # one copy per distinct output
			if len(key) == 3:
				raw.setdefault(key[:2], {}).update(memory=r["memory"], memcheck_timeout=r["timeout"])
			else:
				raw.setdefault(key, {}).update(r)
			if n % 500 == 0:
				print(f"[run] {n}/{len(futs)}", file=sys.stderr)
	# Installed is a property of the column: a program present for some files and absent for others is not one.
	partly = {name for name in missing if any(k[0] == name and "verdict" in v for k, v in raw.items())}
	assert not partly, f"implementations missing for only some tests: {sorted(partly)}"
	raw = {k: v for k, v in raw.items() if k[0] not in missing}
	return raw, missing


def _judge_and_report(args, tests: list[Test], avail: list[Impl], refs: dict[int, str], raw: dict, memcheck: bool) -> None:
	problems = []
	expected: dict[str, dict[int, tuple | None]] = {}
	alts: dict[str, dict[int, list[tuple[str, tuple]]]] = {}
	ref_impls = {ed: next(i for i in avail if i.name == refs[ed]) for ed in EDITIONS}
	for t in tests:
		alts[t.id] = reference_alternatives(ref_impls, args.manifest.parent / t.file, raw[(refs[3], t.id)]["stdout"])
		expected[t.id] = {}
		for ed in EDITIONS:
			r = raw[(refs[ed], t.id)]
			want = r["image"] if r["verdict"] == "accept" else None
			expected[t.id][ed] = want
			got_hash = want[0] if want is not None else None
			if (got_hash is None) != (not t.hashes[ed]) or (got_hash and got_hash != t.hashes[ed][0]):
				problems.append(f"{t.id}: reference (edition {ed}) output {got_hash} does not match the manifest's {t.hashes[ed][:1]}")
		if not args.no_trace:
			# expect.py declares what the file reaches under either edition
			missing = set(t.reaches) - set().union(*(reached_of(raw[(refs[ed], t.id)]["stdout"]) for ed in EDITIONS))
			if not t.reaches:
				problems.append(f"{t.id}: declares no clauses reached")
			elif missing:
				problems.append(f"{t.id}: no longer reaches {', '.join(sorted(missing))}")

	results = {impl.name: {t.id: judge(t, impl, raw[(impl.name, t.id)], expected[t.id], alts[t.id]) for t in tests} for impl in avail}
	clauses = {}
	with open(ROOT / "spec" / "clauses.tsv", newline="") as f:
		for r in csv.DictReader(f, delimiter="\t"):
			clauses[r["id"]] = r["statement"]
	report = {
		"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"host": os.uname().sysname + " " + os.uname().machine,
		"memcheck": memcheck,
		"problems": problems,
		"implementations": [{
			"name": i.name, "project": i.project, "variant": i.variant, "lineage": i.lineage,
			"version": version_of(i), "revision": i.revision, "notes": i.notes, "pixels": i.pixels,
			"max_depth": i.max_depth, "memcheck": memcheck and i.memcheck,
			"summary": summarize(i, results[i.name], tests),
		} for i in avail],
		"tests": [{
			"id": t.id, "file": t.file, "clause": t.clause, "statement": clauses.get(t.clause, ""),
			"description": t.description, "allowed": {str(ed): t.allowed[ed] for ed in EDITIONS},
			"discriminating": pixels_differ(t), "conforms": {str(ed): t.conforms[ed] for ed in EDITIONS},
		} for t in tests],
		"results": {name: {tid: {k: v for k, v in r.items()} for tid, r in rs.items()} for name, rs in results.items()},
	}
	args.out.mkdir(parents=True, exist_ok=True)
	(args.out / "report.json").write_text(json.dumps(report, indent="\t", ensure_ascii=False) + "\n")
	for p in problems:
		print(f"[problem] {p}", file=sys.stderr)
	import report as html_report
	html_report.write(report, args.out / "report.html")
	print(f"[report] {args.out / 'report.json'}, {args.out / 'report.html'}", file=sys.stderr)
	if problems:
		sys.exit(1)


if __name__ == "__main__":
	main()
