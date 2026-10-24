"""Derive test expectations from the reference decoder.

	uv run expect.py                       # refresh decode/manifest.tsv: hashes and reached clauses
										   # (refuses if a file stops reaching a declared clause)
	uv run expect.py --pool DIR FILE...    # copy distinct FILEs into DIR and write DIR/manifest.tsv
	uv run expect.py --pool DIR            # judge DIR's files again and rewrite DIR/manifest.tsv

A test's allowed outcomes follow from what the reference decoder found in the file, through the
`consequence` column of spec/clauses.tsv: a fatal fault means the file must be rejected (and
decoding it is allowed only if that fault's consequence is `either`); recoverable faults mean
the expected pixels pass and a rejection is a should-violation; `either` faults allow both; a
clean file must decode to the expected pixels.

Separately, `conforms2`/`conforms3` say whether the datastream conforms to each edition: the reference
decodes it and records no fault. Validators are judged on that, not on what a decoder must do.
"""
import argparse
import csv
import hashlib
import os
import re
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pam
import run

ROOT = Path(__file__).resolve().parent
FIELDS = ["id", "file", "clause", "reaches", "ed2", "hash2", "conforms2", "ed3", "hash3", "conforms3", "description"]


def consequences() -> dict[str, str]:
	with open(ROOT / "spec" / "clauses.tsv", newline="") as f:
		return {r["id"]: r["consequence"] for r in csv.DictReader(f, delimiter="\t")}


def faults(stdout: str) -> tuple[list[str], list[str]]:
	"""(fatal ids, recovered ids) from the reference decoder's ERR/WARN lines."""
	fatal, recovered = [], []
	for line in stdout.splitlines():
		if line.startswith(("WARN [", "ERR [")):
			cid = line.split("[", 1)[1].split("]", 1)[0]
			(fatal if line.startswith("ERR") else recovered).append(cid)
	return fatal, recovered


class Unjudgeable(Exception):
	"""The reference decoder ran out of time or memory on this file."""


def unreported(raw: dict, cons: dict[str, str]) -> set[str]:
	"""Faults the reference reached without reporting them as WARN or ERR.

	Expectations come from the reported faults alone, so a fault that is only traced would make its file look
	clean: every clause with a consequence other than `none` that the trace holds must also appear in a WARN or
	ERR line, as the line's clause or nested in its message (a fault inside a compressed ancillary chunk).
	The reference's Diagnostics record enforces this at insertion. Keep this boundary check for cached output,
	older reference versions, and future changes to the stdout transport."""
	reported = set(re.findall(r"\[([^\]\s]+#[^\]\s]+)\]", "\n".join(l for l in raw["stdout"].splitlines() if l.startswith(("WARN ", "ERR ")))))
	return {c for c in run.reached_of(raw["stdout"]) if cons.get(c, "none") != "none"} - reported


def outcomes(raw: dict, cons: dict[str, str]) -> str:
	fatal, recovered = faults(raw["stdout"])
	if raw["verdict"] in ("timeout", "oom") or raw["verdict"] == "reject" and "MemoryError" in (raw["detail"] or ""):
		raise Unjudgeable(f"reference {raw['verdict']}: {raw['detail']}")
	if raw["verdict"] != "accept":
		assert raw["verdict"] == "reject", f"reference {raw['verdict']}: {raw['detail']}"
		assert fatal, f"reference rejected without naming a clause: {raw['detail']}"
		return "reject=pass,accept=pass" if all(cons.get(c) == "either" for c in fatal) else "reject=pass"
	# `unaffected` faults make the datastream non-conforming but leave what a decoder must do unchanged.
	kinds = {cons.get(c, "recover") for c in recovered} - {"unaffected"}
	if "either" in kinds:
		return "pixels=pass,reject=pass"
	if kinds:
		return "pixels=pass,reject=should"
	return "pixels=pass"


def conforms(raw: dict) -> str:
	"""Whether the datastream conforms to the edition: the reference decodes it and records no fault.

	What a decoder must do is a separate question (outcomes): a file can be non-conforming and still have
	to decode unchanged. Validators are judged on this one."""
	fatal, recovered = faults(raw["stdout"])
	return "yes" if raw["verdict"] == "accept" and not fatal and not recovered else "no"


def reference_runs(impls: dict[str, run.Impl], file: Path) -> dict[int, dict]:
	return {ed: run.run_one(impls[f"reference-ed{ed}"], file, False) for ed in run.EDITIONS}


def row_for(file: Path, base: Path, impls: dict[str, run.Impl], cons: dict[str, str]) -> dict:
	refs = reference_runs(impls, file)
	row = {"id": file.stem, "file": str(file.relative_to(base))}
	reaches: set[str] = set()
	for ed, raw in refs.items():
		silent = unreported(raw, cons)
		assert not silent, f"{file.name}: the reference (edition {ed}) reached {sorted(silent)} without reporting them"
		row[f"ed{ed}"] = outcomes(raw, cons)
		row[f"hash{ed}"] = raw["image"][0] if raw["image"] else ""
		row[f"conforms{ed}"] = conforms(raw)
		reaches |= run.reached_of(raw["stdout"])
	fatal, recovered = faults(refs[3]["stdout"])
	row["reaches"] = ",".join(sorted(reaches))
	row["clause"] = (fatal + recovered + [""])[0]
	row["description"] = refs[3]["detail"] or ""
	return row


def pool_row(file: Path, base: Path, impls: dict[str, run.Impl], cons: dict[str, str]) -> dict | str:
	"""row_for, or the reason the reference decoder cannot judge this file (it exceeds its limits)."""
	try:
		return row_for(file, base, impls, cons)
	except Unjudgeable as e:
		return str(e)


def write(path: Path, rows: list[dict]) -> None:
	fields = FIELDS + [k for k in (rows[0] if rows else {}) if k not in FIELDS]
	with open(path, "w", newline="") as f:
		w = csv.DictWriter(f, fields, delimiter="\t", lineterminator="\n")
		w.writeheader()
		w.writerows(rows)


def main() -> None:
	run.cap_memory()
	ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
	ap.add_argument("--pool", type=Path, help="build a candidate pool in this directory")
	ap.add_argument("--head", type=Path, default=Path.home() / ".cache/difftest-png/head")
	ap.add_argument("--retrace", action="store_true", help="accept files that no longer reach a declared clause")
	ap.add_argument("files", nargs="*", type=Path, help="files, or - to read one path per line from stdin")
	args = ap.parse_args()
	if args.files == [Path("-")]:
		args.files = [Path(l) for l in sys.stdin.read().splitlines() if l]
	impls = {i.name: i for i in run.load_impls(args.head)}
	cons = consequences()
	if args.pool:
		args.pool.mkdir(parents=True, exist_ok=True)
		seen: dict[str, Path] = {}
		for src in args.files:
			digest = hashlib.sha256(src.read_bytes()).hexdigest()
			if digest in seen:
				continue
			parts = src.resolve().parts
			dst = args.pool / "__".join(parts[parts.index("corpus") + 1:] if "corpus" in parts else parts[-2:])
			assert not dst.exists() or dst.read_bytes() == src.read_bytes(), dst
			shutil.copyfile(src, dst)
			seen[digest] = dst
		files = sorted(seen.values()) if args.files else sorted(p for p in args.pool.iterdir() if p.suffix in (".png", ".apng"))
		with ProcessPoolExecutor(os.cpu_count()) as ex:
			rows = list(ex.map(pool_row, files, [args.pool] * len(files), [impls] * len(files), [cons] * len(files), chunksize=8))
		for f, r in zip(files, rows):
			if isinstance(r, str):
				print(f"[pool] dropped {f.name}: {r}", file=sys.stderr)
				f.unlink()
		rows = [r for r in rows if isinstance(r, dict)]
		assert len(rows) > len(files) // 2, f"the reference judged only {len(rows)} of {len(files)} files"
		write(args.pool / "manifest.tsv", rows)
		print(f"{len(rows)} files -> {args.pool / 'manifest.tsv'}", file=sys.stderr)
		return
	manifest = ROOT / "decode" / "manifest.tsv"
	tests = run.load_manifest(manifest)
	with open(manifest, newline="") as f:
		rows = list(csv.DictReader(f, delimiter="\t"))
	changed, lost = 0, []
	for r, t in zip(rows, tests):
		fresh = row_for(manifest.parent / t.file, manifest.parent, impls, cons)
		for ed in run.EDITIONS:
			r[f"hash{ed}"] = fresh[f"hash{ed}"]
			if fresh[f"ed{ed}"] != r[f"ed{ed}"]:
				print(f"{t.id}: edition {ed} outcomes would be {fresh[f'ed{ed}']} (manifest says {r[f'ed{ed}']}; kept)", file=sys.stderr)
			if fresh[f"conforms{ed}"] != r[f"conforms{ed}"]:
				print(f"{t.id}: edition {ed} conformance would be {fresh[f'conforms{ed}']} (manifest says {r[f'conforms{ed}']}; kept)", file=sys.stderr)
		missing = set(t.reaches) - set(fresh["reaches"].split(","))
		if missing:
			lost.append(f"{t.id}: no longer reaches {', '.join(sorted(missing))}")
		if fresh["reaches"] != r["reaches"]:
			changed += 1
		r["reaches"] = fresh["reaches"]
	for line in lost:
		print(line, file=sys.stderr)
	if lost and not args.retrace:
		sys.exit(f"{len(lost)} tests lost declared clauses; manifest not written (--retrace to accept)")
	write(manifest, rows)
	print(f"{len(rows)} tests refreshed, {changed} with a different trace", file=sys.stderr)


if __name__ == "__main__":
	main()
