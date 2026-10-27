"""Derive test expectations from the reference decoder and the lenses.

	uv run expect.py                       # refresh decode/manifest.tsv from the reference decoder
	uv run expect.py --retrace             # the same, accepting files that no longer reach a declared clause
	uv run expect.py --rename              # the same, renaming files whose prefix no longer matches expect3
	uv run expect.py --pool DIR FILE...    # copy distinct FILEs into DIR and write DIR/manifest.tsv
	uv run expect.py --pool DIR            # judge DIR's files again and rewrite DIR/manifest.tsv

Each file is decoded in-process, once (reading): refpng.decode gives every interpretation of its pixels
and the Diagnostics record, and pnglens reads the record under each lens. The manifest's columns:
- `clause`: the first ERR fault's clause, else the first WARN fault's, among the faults that apply under
  lens 3 if there are any; empty for a file with no fault.
- `reaches`: every clause in the record, coverage and faults. run.py and expect.py refuse a file that stops
  reaching one of them (--retrace accepts it), so a hand edit or an optimizer cannot quietly turn a test
  into a different one.
- `images`: every interpretation as `choices=digest`, joined by `|`, in the decoder's order (no choices
  first, written `none`); choices are `topic:option` joined by `,`; the digest is pam.digest of the
  canonical RGBA16. Empty when no pixels can exist (refpng.NoImage).
- `conforms2`, `expect2`, `conforms3`, `expect3`: each lens's verdict, `yes`/`no` and what a decoder is to
  do with the file (display, recover, may-reject, must-reject).
- `strict`: the strict notes (pnglens.STRICT_NOTES) the file carries, comma-joined.
- `description`: the faults that apply under lens 3, in words; for a file without an image, `no image:`
  and the fault that ended decoding first.

The filename prefix summarises expect3 (PREFIX). Refreshing refuses to write a manifest in which a
prefix disagrees with expect3, naming the files; --rename renames them and rewrites their ids instead.
"""
import argparse
import csv
import hashlib
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import pam
import pnglens
import refpng
import run
from pngdiagnostics import Diagnostics

ROOT = Path(__file__).resolve().parent
FIELDS = ["id", "file", "clause", "reaches", "images", "conforms2", "expect2", "conforms3", "expect3", "strict", "description"]
PICKED = ["source", "picked", "note"]  # pick.py's columns, which a refresh keeps
PREFIX = {"display": "y", "recover": "r", "may-reject": "i", "must-reject": "n"}
assert set(PREFIX) == set(pnglens.STRENGTH)
assert tuple(pnglens.LENSES) == ("2", "3"), "the manifest has one conforms and one expect column per lens"


@dataclass(frozen=True)
class Interpretation:
	choices: tuple[tuple[str, str], ...]
	rgba: np.ndarray  # canonical RGBA16, the form every output is compared in
	digest: str


@dataclass(frozen=True)
class Reading:
	"""One in-process decode of a file, read under every lens."""
	interpretations: tuple[Interpretation, ...]  # in the decoder's order; empty when no pixels can exist
	diagnostics: Diagnostics
	verdicts: dict[str, pnglens.Verdict]  # lens name -> verdict
	no_image: refpng.NoImage | None


def choices_text(choices: tuple[tuple[str, str], ...]) -> str:
	return ",".join(f"{topic}:{option}" for topic, option in choices) or "none"


def reading(file: Path) -> Reading:
	try:
		decoded = refpng.decode(file.read_bytes())
	except refpng.NoImage as e:
		diagnostics, interpretations, no_image = e.diagnostics, (), e
	else:
		diagnostics, no_image = decoded.diagnostics, None
		interpretations = []
		for i in decoded.interpretations:
			rgba = pam.rgba16(pam.parse(refpng.to_pam(i.image)))
			interpretations.append(Interpretation(i.choices, rgba, pam.digest(rgba)))
		interpretations = tuple(interpretations)
		order = [i.choices for i in interpretations]
		assert order and order == sorted(order) and len(set(order)) == len(order), f"{file.name}: interpretations {order}"
	verdicts = {name: pnglens.verdict(diagnostics, lens, no_image is not None) for name, lens in pnglens.LENSES.items()}
	assert all(v.has_image == bool(interpretations) for v in verdicts.values())
	return Reading(interpretations, diagnostics, verdicts, no_image)


def images_text(r: Reading) -> str:
	return "|".join(f"{choices_text(i.choices)}={i.digest}" for i in r.interpretations)


def row_for(file: Path, base: Path) -> dict:
	return row(reading(file), file, base)


def row(r: Reading, file: Path, base: Path) -> dict:
	"""The file's manifest row (FIELDS) from its reading."""
	events = [e for e in r.diagnostics.events if e.level != "INFO"]
	row = {"id": file.stem, "file": str(file.relative_to(base))}
	# Faults that apply under lens 3 first, as the prefix and the description read the file through it.
	ranked = sorted(events, key=lambda e: (not pnglens.LENSES["3"].applies(e.id), e.level != "ERR"))
	row["clause"] = ranked[0].id if ranked else ""
	row["reaches"] = ",".join(sorted(r.diagnostics.reached))
	row["images"] = images_text(r)
	for name, v in r.verdicts.items():
		row[f"conforms{name}"] = "yes" if v.conforms else "no"
		row[f"expect{name}"] = v.expectation
	notes = {v.strict_notes for v in r.verdicts.values()}
	assert len(notes) == 1, f"{file.name}: strict notes differ between lenses: {notes}"
	row["strict"] = ",".join(notes.pop())
	words = pnglens.faults_in_words(r.verdicts["3"]).replace("\n", " ")
	if r.no_image is not None:
		words = f"no image: [{r.no_image.id}] {r.no_image}" + (f" {words}" if words else "")
	row["description"] = words
	return row


def pool_row(file: Path, base: Path) -> dict | str:
	"""row_for, or the reason the reference decoder cannot judge this file (it exhausts memory)."""
	try:
		return row_for(file, base)
	except MemoryError as e:
		return f"reference MemoryError: {e}"


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
	ap.add_argument("--retrace", action="store_true", help="accept files that no longer reach a declared clause")
	ap.add_argument("--rename", action="store_true", help="rename files whose prefix no longer matches expect3")
	ap.add_argument("files", nargs="*", type=Path, help="files, or - to read one path per line from stdin")
	args = ap.parse_args()
	if args.files == [Path("-")]:
		args.files = [Path(l) for l in sys.stdin.read().splitlines() if l]
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
			rows = list(ex.map(pool_row, files, [args.pool] * len(files), chunksize=8))
		for f, r in zip(files, rows):
			if isinstance(r, str):
				print(f"[pool] dropped {f.name}: {r}", file=sys.stderr)
				f.unlink()
		rows = [r for r in rows if isinstance(r, dict)]
		assert len(rows) > len(files) // 2, f"the reference judged only {len(rows)} of {len(files)} files"
		write(args.pool / "manifest.tsv", rows)
		print(f"{len(rows)} files -> {args.pool / 'manifest.tsv'}", file=sys.stderr)
		return
	assert not args.files, "files are given only with --pool"
	manifest = ROOT / "decode" / "manifest.tsv"
	with open(manifest, newline="") as f:
		rows = list(csv.DictReader(f, delimiter="\t"))
	assert len({r["id"] for r in rows}) == len(rows), "duplicate test ids"
	changed, lost, misnamed = 0, [], []
	for n, r in enumerate(rows):
		assert r["file"] == f"{r['id']}.png", f"{r['id']}: file {r['file']}"
		fresh = row_for(manifest.parent / r["file"], manifest.parent)
		missing = set(filter(None, r["reaches"].split(","))) - set(fresh["reaches"].split(","))
		if missing:
			lost.append(f"{r['id']}: no longer reaches {', '.join(sorted(missing))}")
		changed += any(r.get(k) != fresh[k] for k in FIELDS)
		r = rows[n] = fresh | {k: r[k] for k in PICKED if k in r}
		want = PREFIX[r["expect3"]]
		if r["id"].split("_", 1)[0] != want:
			misnamed.append((r, f"{want}_{r['id'].split('_', 1)[1]}"))
	for line in lost:
		print(line, file=sys.stderr)
	for r, name in misnamed:
		print(f"{r['id']}: expect3 is {r['expect3']}, so its name is {name}", file=sys.stderr)
	refused = []
	if lost and not args.retrace:
		refused.append(f"{len(lost)} tests lost declared clauses (--retrace to accept)")
	if misnamed and not args.rename:
		refused.append(f"{len(misnamed)} files have a prefix that disagrees with expect3 (--rename to rename them)")
	if refused:
		sys.exit("; ".join(refused) + "; manifest not written")
	ids = {r["id"] for r in rows}
	for r, name in misnamed:
		assert name not in ids and not (manifest.parent / f"{name}.png").exists(), f"{r['id']}: {name} is taken"
		ids.add(name)
	for r, name in misnamed:
		(manifest.parent / r["file"]).rename(manifest.parent / f"{name}.png")
		print(f"renamed {r['file']} -> {name}.png", file=sys.stderr)
		r.update(id=name, file=f"{name}.png")
	write(manifest, rows)
	print(f"{len(rows)} tests refreshed, {changed} changed, {len(misnamed)} renamed", file=sys.stderr)


if __name__ == "__main__":
	main()
