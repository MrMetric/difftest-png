"""What the PNG specification says about a datastream, read through a lens.

	uv run python pnglens.py in.png                       # both lenses' verdicts in words
	uv run python pnglens.py --lens 2|3 in.png out.pam    # a decoder that follows one lens

With --lens, the file is decoded and judged under that lens: when the expectation is must-reject, or
no pixels exist, every applicable fault is printed as an `ERR [id] message` line and the exit status
is 1; otherwise every applicable fault is printed as a `WARN [id] message` line, the preferred
interpretation (preferred()) is written as a PAM, and `OK` is printed.

A lens is an edition's text plus a set of extensions (LENSES). verdict() reads a Diagnostics
record through the clause table, spec/clauses.tsv:
- a clause applies under a lens if the lens's edition is in its `editions` column, or its
  `extension` column names one of the lens's extensions;
- the record's WARN and ERR events are its faults; INFO events (consequence `none`) are coverage;
- the expectation is the strongest one EXPECTATION gives the applicable faults' consequences, in
  the order of STRENGTH, or `display` when there are none. When the decoder raised NoImage there
  are no reference pixels (`has_image` is false), and the expectation is must-reject or may-reject,
  asserted: a file without an image always carries a fault with one of those consequences;
- the file conforms iff no fault applies. The `subject` column says whom a clause's sentence
  addresses, not what is defective, so it plays no part;
- strict notes are the applicable STRICT_NOTES coverage ids present: facts a strict text-only
  validator may flag although the extension model accepts them;
- rate() rates an interpretation's choices through RATINGS, the same under both lenses; an
  interpretation rates as its weakest choice, and the empty tuple as `recommended`.
"""
import csv
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path

from pngdiagnostics import CONSEQUENCES, Diagnostics

with (Path(__file__).resolve().parent / "spec/clauses.tsv").open(newline="") as f:
	CLAUSES = {r["id"]: r for r in csv.DictReader(f, delimiter="\t")}
assert {id: r["consequence"] for id, r in CLAUSES.items()} == dict(CONSEQUENCES), "pngdiagnostics read another table"

EXPECTATION = {"reject": "must-reject", "either": "may-reject", "recover": "recover",
	"none": "display", "still": "recover", "unaffected": "display"}
STRENGTH = ("must-reject", "may-reject", "recover", "display")  # strongest first
STRICT_NOTES = frozenset({"idot#public-name"})
EXTENSIONS = frozenset({"registered", "eXIf", "APNG", "cICP", "mDCV", "cLLI", "iDOT"})
RATINGS = {("trns", "first"): "arguable", ("trns", "last"): "arguable",
	("plte", "first"): "arguable", ("plte", "last"): "arguable",
	("crc", "dropped"): "recommended", ("crc", "ignored"): "arguable",
	("critical", "ignored"): "arguable"}
RATING_ORDER = ("arguable", "recommended")  # weakest first

EDITIONS = {id: frozenset(int(e) for e in r["editions"].split(",")) for id, r in CLAUSES.items()}
EXTENSION = {id: r["extension"] for id, r in CLAUSES.items()}
assert set(EXPECTATION) == {r["consequence"] for r in CLAUSES.values()}, "a consequence the mapping does not know"
assert set(EXPECTATION.values()) == set(STRENGTH)
assert {r["subject"] for r in CLAUSES.values()} == {"datastream", "decoder", "both"}
assert all(EDITIONS[id] and EDITIONS[id] <= {2, 3} for id in CLAUSES), "editions are 2 and/or 3"
assert set(EXTENSION.values()) == EXTENSIONS | {""}, "every extension has clauses, and no clause names another"
assert all(CONSEQUENCES[id] == "none" for id in STRICT_NOTES), "a strict note is coverage, not a fault"
assert set(RATINGS.values()) == set(RATING_ORDER)


@dataclass(frozen=True)
class Lens:
	edition: int
	extensions: frozenset[str]

	def __post_init__(self):
		assert self.edition in (2, 3) and self.extensions <= EXTENSIONS, self

	def applies(self, id: str) -> bool:
		return self.edition in EDITIONS[id] or EXTENSION[id] in self.extensions


LENSES = {"2": Lens(2, EXTENSIONS), "3": Lens(3, EXTENSIONS)}


@dataclass(frozen=True)
class Fault:
	id: str
	level: str  # WARN or ERR
	message: str  # as Diagnostics prints it, with any cause clause first
	statement: str
	applies: bool  # under the verdict's lens


def rate(choices: tuple[tuple[str, str], ...]) -> str:
	"""`recommended` or `arguable`: the weakest rating among the choices."""
	assert isinstance(choices, tuple) and all(c in RATINGS for c in choices), f"unknown choice in {choices!r}"
	return min((RATINGS[c] for c in choices), key=RATING_ORDER.index, default="recommended")


@dataclass(frozen=True)
class Verdict:
	conforms: bool
	expectation: str
	faults: tuple[Fault, ...]
	strict_notes: tuple[str, ...]
	has_image: bool  # false when the decoder raised NoImage: nothing to compare a decoder's pixels with

	def rate(self, choices: tuple[tuple[str, str], ...]) -> str:
		return rate(choices)


def verdict(diagnostics: Diagnostics, lens: Lens, no_image: bool = False) -> Verdict:
	"""What the specification says about the recorded datastream under the lens. `no_image`: the decoder raised NoImage."""
	events = diagnostics.events
	assert not no_image or any(e.level == "ERR" for e in events), "NoImage is recorded as an ERR event"
	faults = tuple(Fault(e.id, e.level, Diagnostics.message(e), CLAUSES[e.id]["statement"], lens.applies(e.id))
		for e in events if e.level != "INFO")
	assert all(CONSEQUENCES[f.id] != "none" for f in faults), "a fault is never coverage"
	applicable = [f for f in faults if f.applies]
	expected = min((EXPECTATION[CONSEQUENCES[f.id]] for f in applicable), key=STRENGTH.index, default="display")
	conforms = not applicable
	notes = tuple(sorted({e.id for e in events if e.id in STRICT_NOTES and lens.applies(e.id)}))
	assert not no_image or expected in ("must-reject", "may-reject"), f"no image, yet the lens expects {expected}"
	return Verdict(conforms, expected, faults, notes, not no_image)


def faults_in_words(v: Verdict) -> str:
	"""One line per applicable fault: level, clause id, the clause's statement, and what was found."""
	return "\n".join(f"{f.level} [{f.id}] {f.statement} Found: {f.message}" for f in v.faults if f.applies)


def preferred(interpretations):
	"""The interpretation a decoder following the specification produces: the first rated `recommended`,
	else the first. Interpretations carry `.choices`; their order is the decoder's."""
	assert interpretations
	return next((i for i in interpretations if rate(i.choices) == "recommended"), interpretations[0])


def as_decoder(lens: Lens, argv: list[str]) -> int:
	import refpng
	try:
		decoded = refpng.decode(Path(argv[0]).read_bytes())
	except refpng.NoImage as e:
		diagnostics, interpretations, no_image = e.diagnostics, [], True
	else:
		diagnostics, interpretations, no_image = decoded.diagnostics, decoded.interpretations, False
	v = verdict(diagnostics, lens, no_image)
	applicable = [f for f in v.faults if f.applies]
	if no_image or v.expectation == "must-reject":
		assert applicable, "a rejection names its faults"
		for f in applicable:
			print(f"ERR [{f.id}] {f.message}")
		return 1
	for f in applicable:
		print(f"WARN [{f.id}] {f.message}")
	with open(argv[1], "wb") as out:
		out.write(refpng.to_pam(preferred(interpretations).image))
	print("OK")
	return 0


def _choices(choices: tuple[tuple[str, str], ...]) -> str:
	return ",".join(f"{topic}:{option}" for topic, option in choices) or "none"


def main(argv: list[str]) -> int:
	if len(argv) == 4 and argv[0] == "--lens" and argv[1] in LENSES:
		return as_decoder(LENSES[argv[1]], argv[2:])
	if len(argv) != 1:
		print("usage: pnglens.py in.png | pnglens.py --lens 2|3 in.png out.pam", file=sys.stderr)
		return 2
	# Imported here so that importing the lens does not import the decoder.
	import refpng
	assert "edition" not in inspect.signature(refpng.decode).parameters and hasattr(refpng, "NoImage"), \
		"refpng.decode still has the decode(data, edition) signature; pnglens needs decode(data) -> Decoded, raising NoImage"
	try:
		decoded = refpng.decode(Path(argv[0]).read_bytes())
	except refpng.NoImage as e:
		print(f"no image: [{e.id}] {e}")
		diagnostics, interpretations, no_image = e.diagnostics, [], True
	else:
		diagnostics, interpretations, no_image = decoded.diagnostics, decoded.interpretations, False
	for name, lens in LENSES.items():
		v = verdict(diagnostics, lens, no_image)
		print(f"lens {name}: {'conforms' if v.conforms else 'does not conform'}; expectation {v.expectation}"
			+ ("" if v.has_image else "; no reference pixels"))
		if v.faults:
			print(faults_in_words(v) or "(no fault applies under this lens)")
		for note in v.strict_notes:
			print(f"strict [{note}] {CLAUSES[note]['statement']}")
		for i in interpretations:
			print(f"interpretation {_choices(i.choices)}: {v.rate(i.choices)}")
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
