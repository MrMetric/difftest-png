"""Choose the suite from a candidate pool that run.py has judged.

	uv run pick.py out/pool [--exclude POOL-ID ...]    # writes decode/*.png and decode/manifest.tsv

The suite is every conformance test (the hand-made corpora: synth, probe, new, fix) and every file any
implementation fails, plus the smallest file reaching each clause or rule the reference decoder reports
that no kept file reaches.
Files are renamed `<y|n|r|i>_<name>.png` from what lens 3 expects a decoder to do with them
(expect.PREFIX): y display, r recover, i may reject, n must reject.

--exclude drops candidates whose only distinguishing outcome is noise, such as a result that hinges
on an implementation reading uninitialised memory.
"""
import csv
import json
import re
import shutil
import sys
from pathlib import Path

import expect

ROOT = Path(__file__).resolve().parent
CONFORMANCE = ("synth", "probe", "new", "fix")


def short_name(source: str) -> str:
	parts = source.removesuffix(".png").removesuffix(".apng").split("__")
	corpus, rest = parts[0], parts[-1]
	if corpus in ("synth", "probe", "new", "fix"):
		return rest
	return f"{corpus.split('-')[0]}-{rest}"


def main() -> None:
	pool = Path(sys.argv[1])
	excluded = set(sys.argv[3:]) if sys.argv[2:3] == ["--exclude"] else set()
	assert len(sys.argv) == 2 or excluded, "usage: pick.py POOL [--exclude POOL-ID ...]"
	report = json.loads((pool / "report.json").read_text())
	with open(pool / "manifest.tsv", newline="") as f:
		rows = {r["id"]: r for r in csv.DictReader(f, delimiter="\t")}
	impls = [i["name"] for i in report["implementations"] if i["project"] != "reference"]
	size = {tid: (pool / rows[tid]["file"]).stat().st_size for tid in rows}

	def vector(tid: str) -> tuple:
		out = []
		for name in impls:
			r = report["results"][name][tid]
			o = r["outcome"] if isinstance(r["outcome"], str) else json.dumps(r["outcome"], sort_keys=True)
			tag = r.get("label") if isinstance(r.get("label"), dict) else {}
			# only the surprising alpha-0 behaviours make a pattern; "preserved" and "nothing to judge" do not
			alpha0 = tag.get("alpha0") if tag.get("alpha0") in ("zeroed", "altered") else None
			out.append((o, tuple(sorted(r["severity"].items())), alpha0, tuple(tag.get("choices", ()))))
		return tuple(out)

	def failed(v: tuple) -> bool:  # under some lens
		return any(s != "pass" for e in v for _, s in e[1])

	assert excluded <= set(rows), f"not in the pool: {sorted(excluded - set(rows))}"
	order = sorted((t for t in rows if t not in excluded), key=lambda t: (size[t], t))
	chosen: dict[str, str] = {}  # test id -> why
	for tid in order:
		if rows[tid]["file"].split("__")[0] in CONFORMANCE:
			chosen[tid] = "conformance"
		elif failed(vector(tid)):
			chosen[tid] = "failed"
	reached = set()
	for tid in chosen:
		reached |= set(filter(None, rows[tid]["reaches"].split(",")))
	for tid in order:
		new = set(filter(None, rows[tid]["reaches"].split(","))) - reached
		if new:
			chosen.setdefault(tid, "clause")
			reached |= new

	dest = ROOT / "decode"
	for old in dest.glob("*.png"):
		old.unlink()
	out_rows, names = [], set()
	for tid in sorted(chosen, key=lambda t: (rows[t]["clause"] or "~", t)):
		r = dict(rows[tid])
		prefix = expect.PREFIX[r["expect3"]]
		name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{prefix}_{short_name(r['file'])}").strip("-")
		if name in names:  # two corpora's files share a short name: the pool id is unique
			name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{prefix}_{r['file'].removesuffix('.png')}").strip("-")
		assert name not in names, name
		names.add(name)
		shutil.copyfile(pool / r["file"], dest / f"{name}.png")
		r.update(id=name, file=f"{name}.png", source=rows[tid]["file"], picked=chosen[tid])
		out_rows.append(r)
	expect.write(dest / "manifest.tsv", out_rows)
	print(f"{len(rows)} candidates, {len(chosen)} tests: {sum(1 for c in chosen.values() if c == 'conformance')} conformance, "
		f"{sum(1 for c in chosen.values() if c == 'failed')} failed by some implementation, "
		f"{sum(1 for c in chosen.values() if c == 'clause')} for clause coverage only, "
		f"{sum(size[t] for t in chosen)} bytes", file=sys.stderr)


if __name__ == "__main__":
	main()
