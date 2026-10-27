"""Run the PNG test suite against every implementation available here and write the report.

	uv run run.py [impl ...] [--head DIR] [--jobs N] [--no-memcheck] [--no-build] [--out DIR]
				  [--reuse [--rerun IMPL ...]]

Implementations come from impls.toml; a column whose program is missing is skipped with a
notice. Each (implementation, test) runs under address-space, file-size and wall-clock limits;
native columns also run under valgrind memcheck. Every run is fresh unless --reuse is given: then
results stored by earlier runs are used for every column except the reference decoder and those
named in --rerun (name a column there after rebuilding or upgrading it).
The whole run sits in a systemd scope capped at half the machine's memory.

Judging decodes each test file in-process once (expect.reading): every interpretation of its pixels and
each lens's verdict. Per test, the columns' accepted outputs are bucketed by their pixels, and each bucket
is labelled with the interpretations it matches under the equivalences of compare(): exact, the colour
under fully transparent pixels zeroed or altered (`alpha0`), and, for a column whose max_depth is 8, a
16-to-8 reduction (`16to8`). A bucket that matches no interpretation is unexplained. Every outcome is then
scored under each lens separately (score()), and out/report.json plus out/report.html are written.
"""
import argparse
import base64
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from compression import zstd
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

import chunkwalk
import execute
import expect
import pnglens
import report as html_report
from execute import child_env, run_one

ROOT = Path(__file__).resolve().parent
LENSES = tuple(pnglens.LENSES)
# The reference decoder read through each lens (pnglens.py --lens): it passes every test under its own lens.
REFERENCES = {lens: f"reference-{lens}" for lens in LENSES}
FATAL = ("crash", "timeout", "oom", "memory", "shim")  # always a must-violation
# Not a performance limit: no PNG decoder should need this long, and slow decodes that do finish keep their
# real outcome instead of becoming a timeout when the machine is busy.
TIMEOUT_S = 60.0
THUMBNAIL_MAX = 128  # report.json carries an image as a PNG when neither side is longer


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
	images: str  # the manifest's `images`: every interpretation's choices and digest
	conforms: dict[str, bool]  # lens -> whether the datastream conforms (what validators are judged on)
	expectation: dict[str, str]  # lens -> what a decoder is to do with the file
	strict: str  # the strict notes, comma-joined
	description: str
	note: str  # synth.py's account of the file
	about: list[str]  # the clauses the case is about, as synth.py names them
	family: str  # the generator section that wrote it
	explains: dict[str, str]  # digest -> what a decoder whose output has those pixels did (synth.py's explanations)


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


def load_manifest(path: Path) -> list[Test]:
	tests = []
	with open(path, newline="") as f:
		for r in csv.DictReader(f, delimiter="\t"):
			assert all(r[f"expect{lens}"] in pnglens.STRENGTH for lens in LENSES), f"{r['id']}: bad expectation"
			tests.append(Test(
				id=r["id"], file=r["file"], clause=r["clause"],
				reaches=[c for c in r["reaches"].split(",") if c], images=r["images"],
				conforms={lens: {"yes": True, "no": False}[r[f"conforms{lens}"]] for lens in LENSES},
				expectation={lens: r[f"expect{lens}"] for lens in LENSES}, strict=r["strict"],
				description=r["description"], note=r["note"], about=[c for c in r["about"].split(",") if c], family=r["family"],
				explains=dict(item.split(":", 1) for item in r["explains"].split("|") if item),
			))
			assert tests[-1].note and tests[-1].family, f"{r['id']}: no note or family (uv run synth.py)"
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


def compare(max_depth: int, got: np.ndarray, maxval: int, want: np.ndarray) -> tuple[bool, dict]:
	"""Do the decoded pixels match the expected ones? Returns (match, properties of the match).
	`max_depth` and `maxval` are the column's and its output's: only an 8-bit-only column may reduce 16 bits."""
	props = {}
	if got.shape != want.shape:
		return False, props
	if max_depth == 8 and maxval <= 255 and want.max(initial=0) > 0 and np.any(want % 257):
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


def has_hidden_colour(a: np.ndarray) -> bool:
	"""Does the image hold a fully transparent pixel whose colour is not black?"""
	return bool(np.any((a[:, :, 3] == 0) & np.any(a[:, :, :3] != 0, axis=2)))


def label(reading: expect.Reading, image: tuple[str, tuple, bytes], maxval: int, max_depth: int) -> tuple[dict | str | None, str | None, str | None]:
	"""(label, rating, mismatch) of an accepted output. The label lists the choices of every interpretation the
	pixels match, with the properties of the match to the best-rated one, which gives the rating (the first in
	the decoder's order among equals); None when they match none, `unverifiable` when the file has no image.
	`mismatch`, only with a None label, says how the pixels differ from the preferred interpretation's: the two
	shapes, or how many pixels differ in any channel, compared exactly."""
	if not reading.interpretations:
		return "unverifiable", None, None
	found = []
	for i in reading.interpretations:
		if image[0] == i.digest:  # identical pixels: no array work beyond the alpha-0 question
			ok, props = True, ({"alpha0": "preserved"} if has_hidden_colour(i.rgba) else {})
		else:
			ok, props = compare(max_depth, pixels(image), maxval, i.rgba)
		if ok:
			found.append((i, props))
	if not found:
		got, want = pixels(image), pnglens.preferred(reading.interpretations).rgba
		if got.shape != want.shape:
			return None, None, f"{got.shape[1]}x{got.shape[0]} instead of {want.shape[1]}x{want.shape[0]}"
		differ = int(np.count_nonzero((got != want).any(axis=2)))
		assert differ, "pixels identical to the preferred interpretation's matched none"
		return None, None, f"{differ} of {want.shape[0] * want.shape[1]} pixels differ"
	best, props = max(found, key=lambda m: pnglens.RATING_ORDER.index(pnglens.rate(m[0].choices)))
	return {"choices": [expect.choices_text(i.choices) for i, _ in found]} | props, pnglens.rate(best.choices), None


def score(impl: Impl, verdict: str, lens: pnglens.Verdict, tag: dict | str | None) -> tuple[str, str, bool]:
	"""(outcome, severity, strict) of one run under one lens. `strict`: a validator flagged a file that conforms
	only because an extension's definition takes precedence over a convention it collides with."""
	if verdict in FATAL:
		return verdict, "must", False
	assert verdict in ("accept", "reject"), verdict
	if not impl.pixels:  # a validator is judged on whether the datastream conforms
		if verdict == "accept":
			return ("pixels", "pass", False) if lens.conforms else ("accept", "must", False)
		if not lens.conforms:
			return "reject", "pass", False
		return "reject", "pass" if lens.strict_notes else "must", bool(lens.strict_notes)
	if verdict == "reject":
		return "reject", {"display": "must", "recover": "should"}.get(lens.expectation, "pass"), False
	if lens.expectation == "must-reject":  # an invalid file accepted, whatever its pixels (the label says which)
		return "accept", "must", False
	if tag is None:
		return "wrong", "must", False
	if tag == "unverifiable":
		assert lens.expectation == "may-reject", lens
		return "accept", "pass", False
	return "pixels", "pass", False


def judge(impl: Impl, raw: dict, reading: expect.Reading, labelled: tuple[dict | str | None, str | None, str | None] | None) -> dict:
	"""One run's outcome and its severity under each lens. `labelled` is label() of an accepted decoder output."""
	v = raw["verdict"]
	res = {"detail": raw["detail"]}
	if raw.get("memory"):
		res["memory"] = raw["memory"]  # in addition to the outcome of the normal run, which it does not hide
	if raw.get("memcheck_timeout") and v not in FATAL:
		v = "timeout"
		res["detail"] = f"killed after {execute.MEMCHECK_TIMEOUT_S:g} s under valgrind"
	tag = None
	if v == "accept" and impl.pixels:
		assert labelled is not None
		tag, rating, mismatch = labelled
		res["label"] = tag
		if rating:
			res["rating"] = rating
		assert (mismatch is not None) == (tag is None), (tag, mismatch)
		if mismatch:
			res["mismatch"] = mismatch  # an unexplained output: every `wrong` outcome, and an invalid file's accept
	outcome, sev = {}, {}
	for lens, verdict in reading.verdicts.items():
		outcome[lens], sev[lens], strict = score(impl, v, verdict, tag)
		if raw.get("memory"):
			sev[lens] = "must"
		if strict:
			res["strict"] = True
	res["severity"] = sev
	outcomes = set(outcome.values())
	res["outcome"] = outcomes.pop() if len(outcomes) == 1 else outcome
	if raw["warning"] and v == "accept":
		res["warning"] = raw["warning"]
	return res


ACCEPTED = ("pixels", "wrong", "accept")  # the outcomes of an accepted file (a validator's acceptance is `pixels`)


def judge_test(impls: list[Impl], raw: dict[str, dict], reading: expect.Reading) -> dict[str, dict]:
	"""judge() of every column's run on one test (`raw`: column name -> run), labelling each accepted decoder
	output once per distinct input of label()."""
	labelled: dict[tuple, tuple] = {}
	out = {}
	for impl in impls:
		r = raw[impl.name]
		key = None
		if r["verdict"] == "accept" and impl.pixels:
			key = (r["image"][0], r["maxval"], impl.max_depth)
			if key not in labelled:
				labelled[key] = label(reading, r["image"], r["maxval"], impl.max_depth)
		out[impl.name] = judge(impl, r, reading, labelled.get(key))
	return out


def summarize(results: dict[str, dict], conforms: dict[str, dict[str, bool]]) -> dict:
	"""Per lens, how many tests the column passes and fails, and on how many files that do not conform it
	was silent (accepted them without a warning; no severity, an observation); the properties of its
	matches; and per choice topic, how many tests it resolved with each option (the option every
	interpretation it matched shares). `conforms`: test id -> lens -> whether the file conforms."""
	s: dict = {lens: {"must": 0, "should": 0, "pass": 0, "silent": 0} for lens in LENSES}
	for tid, r in results.items():
		for lens in LENSES:
			s[lens][r["severity"][lens]] += 1
			outcome = r["outcome"] if isinstance(r["outcome"], str) else r["outcome"][lens]
			if outcome in ACCEPTED and not conforms[tid][lens] and not r.get("warning"):
				s[lens]["silent"] += 1
	labels = [r["label"] for r in results.values() if isinstance(r.get("label"), dict)]
	for prop in ("alpha0", "16to8"):
		seen = sorted({l[prop] for l in labels if prop in l})
		if seen:
			s[prop] = seen
	strict = sum(1 for r in results.values() if r.get("strict"))
	if strict:
		s["strict"] = strict
	choices: dict[str, dict[str, int]] = {}
	for l in labels:
		shared = set.intersection(*(set() if c == "none" else set(c.split(",")) for c in l["choices"]))
		for item in sorted(shared):
			topic, option = item.split(":")
			choices.setdefault(topic, {}).setdefault(option, 0)
			choices[topic][option] += 1
	s["choices"] = choices
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
	if args.impls:
		wanted = set(args.impls)
		impls = [i for i in impls if i.name in wanted or i.project in wanted or i.name in REFERENCES.values()]
	if not args.no_build:
		build(impls, set(args.rebuild) or ({"all"} if args.rebuild == [] and "--rebuild" in sys.argv else set()))
	avail = []
	for impl in impls:
		why = unavailable(impl)
		if why:
			print(f"[skip] {impl.name}: {why}", file=sys.stderr)
		else:
			avail.append(impl)
	assert set(REFERENCES.values()) <= {i.name for i in avail}, "the reference decoder must be runnable"
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
	assert set(REFERENCES.values()) <= {i.name for i in avail}, "the reference decoder must be runnable"
	print(f"[run] {len(raw)} results in {time.monotonic() - t0:.0f} s", file=sys.stderr)
	_judge_and_report(args, tests, avail, raw, memcheck)


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


def load_spec() -> tuple[dict[str, dict], dict[str, dict]]:
	"""(clauses, sections): spec/clauses.tsv by id, its `section` split into tokens, and spec/sections.tsv by token.
	Every token of every clause's `section`, and every reference in every `says` (report.references), has a row."""
	with open(ROOT / "spec" / "sections.tsv", newline="") as f:
		rows = list(csv.DictReader(f, delimiter="\t"))
	sections = {r["token"]: {"label": r["label"], "url": r["url"]} for r in rows}
	assert len(sections) == len(rows), "spec/sections.tsv: duplicate tokens"
	clauses = {}
	with open(ROOT / "spec" / "clauses.tsv", newline="") as f:
		for r in csv.DictReader(f, delimiter="\t"):
			tokens = [t for group in r["section"].split("; ") for t in group.split(", ")]
			unknown = [t for t in tokens + html_report.references(r["says"]) if t not in sections]
			assert not unknown, f"spec/clauses.tsv {r['id']}: no row in spec/sections.tsv for {unknown}"
			clauses[r["id"]] = {"section": tokens, "says": r["says"], "statement": r["statement"], "consequence": r["consequence"]}
	return clauses, sections


def scrub(res: dict, head: Path) -> None:
	"""Remove this machine's paths from the strings of a result: the repository root, the directory of HEAD
	builds (written {head}/, as impls.toml does) and the home directory (written ~/)."""
	prefixes = ((str(ROOT) + "/", ""), (str(head) + "/", "{head}/"), (str(head.resolve()) + "/", "{head}/"), (str(Path.home()) + "/", "~/"))
	for k in ("detail", "warning", "memory"):
		if isinstance(res.get(k), str):
			for path, short in prefixes:
				res[k] = res[k].replace(path, short)


def thumbnail(rgba: np.ndarray) -> dict:
	"""report.json's `images` entry for one decoded image: its size, and when neither side is longer than
	THUMBNAIL_MAX, `png`: base64 of an RGBA 8-bit PNG of the high byte of each sample. A decoder's output
	may be empty (no rows or no columns), which has no PNG."""
	h, w, channels = rgba.shape
	assert channels == 4 and rgba.dtype == np.uint16, (rgba.shape, rgba.dtype)
	out = {"width": w, "height": h}
	if 0 < w <= THUMBNAIL_MAX and 0 < h <= THUMBNAIL_MAX:
		im = Image.fromarray((rgba >> 8).astype(np.uint8))
		assert im.mode == "RGBA", im.mode
		buf = io.BytesIO()
		im.save(buf, "PNG")
		out["png"] = base64.b64encode(buf.getvalue()).decode("ascii")
	return out


def _judge_and_report(args, tests: list[Test], avail: list[Impl], raw: dict, memcheck: bool) -> None:
	problems = []
	results: dict[str, dict[str, dict]] = {impl.name: {} for impl in avail}
	report_tests = []
	clauses, sections = load_spec()
	images: dict[str, dict] = {}
	derived = [k for k in expect.FIELDS if k not in ("id", "file", "clause", "reaches", "description")]
	for t in tests:
		file = args.manifest.parent / t.file
		reading = expect.reading(file)
		fresh = expect.row(reading, file, args.manifest.parent)
		declared = {"images": t.images, "strict": t.strict} | {f"conforms{lens}": "yes" if t.conforms[lens] else "no" for lens in LENSES} \
			| {f"expect{lens}": t.expectation[lens] for lens in LENSES}
		stale = [k for k in derived if declared[k] != fresh[k]]
		if stale:
			problems.append(f"{t.id}: the manifest's {', '.join(stale)} differ from the reference decoder's (uv run expect.py)")
		if not args.no_trace:
			missing = set(t.reaches) - reading.diagnostics.reached
			if not t.reaches:
				problems.append(f"{t.id}: declares no clauses reached")
			elif missing:
				problems.append(f"{t.id}: no longer reaches {', '.join(sorted(missing))}")
		test_raw = {impl.name: raw[(impl.name, t.id)] for impl in avail}
		judged = judge_test(avail, test_raw, reading)
		for res in judged.values():
			scrub(res, args.head)
		buckets: dict[tuple, dict] = {}
		for impl in avail:
			res = results[impl.name][t.id] = judged[impl.name]
			if "label" in res:
				digest = test_raw[impl.name]["image"][0]
				tag = res["label"] if res["label"] is not None else "unexplained"
				b = buckets.setdefault((digest, json.dumps(tag, sort_keys=True)), {"label": tag, "digest": digest, "columns": []})
				b["columns"].append(impl.name)
				if digest not in images:
					images[digest] = thumbnail(pixels(test_raw[impl.name]["image"]))
		for i in reading.interpretations:
			if i.digest not in images:
				images[i.digest] = thumbnail(i.rgba)
		for b in buckets.values():
			if b["digest"] in t.explains:
				b["explanation"] = t.explains[b["digest"]]
		for lens, name in REFERENCES.items():
			ref, verdict = judged[name], reading.verdicts[lens]
			outcome = ref["outcome"] if isinstance(ref["outcome"], str) else ref["outcome"][lens]
			image = test_raw[name]["image"] if outcome == "pixels" else None
			want = pnglens.preferred(reading.interpretations).digest if verdict.expectation != "must-reject" and verdict.has_image else None
			if ref["severity"][lens] != "pass" or (image[0] if image else None) != want:
				problems.append(f"{t.id}: {name} ({outcome}, {ref['severity'][lens]} under lens {lens}) is not the reference "
					f"decoder read through lens {lens} ({'its preferred interpretation' if want else 'a rejection'})")
		assert all(c in clauses for c in t.about + [t.clause] if c), (t.id, t.about, t.clause)
		report_tests.append({
			"id": t.id, "file": t.file, "clause": t.clause, "statement": clauses[t.clause]["statement"] if t.clause else "",
			"description": t.description, "note": t.note, "about": t.about, "family": t.family, "chunks": chunkwalk.walk(file.read_bytes()),
			"expectation": {lens: v.expectation for lens, v in reading.verdicts.items()},
			"conforms": {lens: v.conforms for lens, v in reading.verdicts.items()},
			"strict": [n for n in t.strict.split(",") if n],
			"interpretations": {expect.choices_text(i.choices): i.digest for i in reading.interpretations},
			"preferred": pnglens.preferred(reading.interpretations).digest if reading.interpretations else None,
			"buckets": sorted(({"label": b["label"], "digest": b["digest"], "count": len(b["columns"]), "columns": b["columns"]}
				| ({"explanation": b["explanation"]} if "explanation" in b else {})
				for b in buckets.values()), key=lambda b: (-b["count"], b["digest"])),
		})
	report = {
		"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"host": os.uname().sysname + " " + os.uname().machine,
		"memcheck": memcheck,
		"lenses": list(LENSES),
		"problems": problems,
		"implementations": [{
			"name": i.name, "project": i.project, "variant": i.variant, "lineage": i.lineage,
			"version": version_of(i), "revision": i.revision, "notes": i.notes, "pixels": i.pixels,
			"max_depth": i.max_depth, "memcheck": memcheck and i.memcheck,
			"summary": summarize(results[i.name], {t["id"]: t["conforms"] for t in report_tests}),
		} for i in avail],
		"tests": report_tests,
		"results": results,
	}
	used = {c: clauses[c] for t in report_tests for c in t["about"] + [t["clause"]] if c}
	report["clauses"] = used
	report["sections"] = {tok: sections[tok] for c in used.values() for tok in c["section"] + html_report.references(c["says"])}
	report["images"] = images
	report["ratings"] = {f"{topic}:{option}": rating for (topic, option), rating in pnglens.RATINGS.items()}
	report["expectations"] = list(pnglens.STRENGTH)  # strongest first
	text = json.dumps(report, indent="\t", ensure_ascii=False) + "\n"
	assert str(Path.home()) not in text, f"report.json holds a path under {Path.home()}"
	args.out.mkdir(parents=True, exist_ok=True)
	(args.out / "report.json").write_text(text)
	for p in problems:
		print(f"[problem] {p}", file=sys.stderr)
	html_report.write(report, args.out / "report.html")
	print(f"[report] {args.out / 'report.json'}, {args.out / 'report.html'}", file=sys.stderr)
	if problems:
		sys.exit(1)


if __name__ == "__main__":
	main()
