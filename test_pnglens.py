"""Plain-assert tests of the lens, on Diagnostics records built directly (no decoder).

	uv run python test_pnglens.py
"""
import sys

import pnglens
from pngdiagnostics import CONSEQUENCES, Diagnostics


def record(*events):
	"""A record holding the events: a bare id is coverage, (id, level) a fault with a message."""
	d = Diagnostics()
	for e in events:
		if isinstance(e, str):
			d.record(e)
		else:
			d.record(*e, f"deliberate {e[0]}")
	return d


def judged(d, no_image=False):
	return {name: pnglens.verdict(d, lens, no_image) for name, lens in pnglens.LENSES.items()}


def assert_both(d, conforms, expectation, no_image=False):
	verdicts = judged(d, no_image)
	for name, v in verdicts.items():
		assert (v.conforms, v.expectation) == (conforms, expectation), (name, v)
	return verdicts


def test_lenses():
	assert set(pnglens.LENSES) == {"2", "3"}
	assert pnglens.LENSES["2"] == pnglens.Lens(2, pnglens.EXTENSIONS)
	assert pnglens.LENSES["3"] == pnglens.Lens(3, pnglens.EXTENSIONS)
	assert pnglens.EXTENSION["idot#public-name"] == "iDOT"
	assert CONSEQUENCES["idot#public-name"] == "none"


def test_edition_only_text_clauses():
	text = {id for id in pnglens.CLAUSES if not pnglens.EXTENSION[id]}
	# Outside the extensions, the editions' texts differ in two places: a palette index past PLTE, and the
	# iTXt language tag grammar.
	assert {id: pnglens.EDITIONS[id] for id in text if pnglens.EDITIONS[id] != {2, 3}} == {
		"11.2.2#index-in-range": {2}, "13.1#index-opaque-black": {3},
		"11.3.3.4#language-rfc3066": {2}, "11.3.3.4#language": {3}}


def test_clean():
	verdicts = assert_both(record("11.2.1#rgb8", "9.2#filter-none"), True, "display")
	for v in verdicts.values():
		assert v.faults == () and v.strict_notes == ()
		assert pnglens.faults_in_words(v) == ""


def test_recover():
	# 5.6#single-ancillary: recover, datastream.
	verdicts = assert_both(record("11.2.1#rgb8", ("5.6#single-ancillary", "WARN")), False, "recover")
	for v in verdicts.values():
		assert [(f.id, f.level, f.applies) for f in v.faults] == [("5.6#single-ancillary", "WARN", True)]
		assert v.faults[0].statement == pnglens.CLAUSES["5.6#single-ancillary"]["statement"]


def test_either():
	# 11.2.2#forbidden: either, datastream.
	assert_both(record(("11.2.2#forbidden", "WARN")), False, "may-reject")


def test_reject():
	# 11.2.3#consecutive: reject, datastream.
	assert_both(record(("11.2.3#consecutive", "ERR")), False, "must-reject")
	# 9.2#filter-types: reject, both; the decoder raised NoImage: no reference pixels, expectation from the fault.
	for v in assert_both(record(("9.2#filter-types", "ERR")), False, "must-reject", no_image=True).values():
		assert not v.has_image
	# 13.1#data-short: either; a short image stream has no image, and accepting or rejecting both pass.
	for v in assert_both(record(("13.1#data-short", "ERR")), False, "may-reject", no_image=True).values():
		assert not v.has_image
	assert all(v.has_image for v in judged(record(("11.2.3#consecutive", "ERR"))).values())
	try:
		pnglens.verdict(record(("5.6#single-ancillary", "WARN")), pnglens.LENSES["3"], no_image=True)
	except AssertionError as e:
		assert "ERR" in str(e)
	else:
		raise AssertionError("NoImage accepted without its ERR event")


def test_strongest_wins():
	# unaffected (5.4#reserved-bit) < recover (5.6#single-ancillary) < either (11.2.2#forbidden) < reject.
	faults = [("5.4#reserved-bit", "WARN"), ("5.6#single-ancillary", "WARN")]
	assert_both(record(faults[0]), False, "display")
	assert_both(record(*faults), False, "recover")
	faults.append(("11.2.2#forbidden", "WARN"))
	assert_both(record(*faults), False, "may-reject")
	faults.append(("11.2.3#consecutive", "ERR"))
	assert_both(record(*faults), False, "must-reject")
	assert_both(record(*reversed(faults)), False, "must-reject")


def test_subject():
	# 13.1#crc-ancillary: either, subject decoder. The sentence addresses the decoder; the bad CRC is still a
	# non-conforming datastream.
	assert pnglens.CLAUSES["13.1#crc-ancillary"]["subject"] == "decoder"
	assert_both(record(("13.1#crc-ancillary", "WARN")), False, "may-reject")


def test_apng_extension():
	# 11.3.6.1#num-frames: still, editions 3, extension APNG; it applies under lens 2 through the extension.
	# 13.1 says a decoder should display the static image, so rejecting is a should-violation: recover.
	assert pnglens.EDITIONS["11.3.6.1#num-frames"] == {3} and pnglens.EXTENSION["11.3.6.1#num-frames"] == "APNG"
	verdicts = assert_both(record("13.1#apng-fallback", ("11.3.6.1#num-frames", "WARN")), False, "recover")
	for v in verdicts.values():
		assert [f.applies for f in v.faults] == [True]
		assert "[11.3.6.1#num-frames]" in pnglens.faults_in_words(v)
	assert not pnglens.Lens(2, pnglens.EXTENSIONS - {"APNG"}).applies("11.3.6.1#num-frames")


def test_idot_strict_note():
	# idot#public-name: none, extension iDOT. Coverage, so the file conforms; the strict note is exposed.
	verdicts = assert_both(record("idot#public-name", "11.2.1#rgb8"), True, "display")
	for v in verdicts.values():
		assert v.strict_notes == ("idot#public-name",) and v.faults == ()


def test_edition_3_only():
	# 13.1#index-opaque-black: recover, editions 3, no extension. It does not apply under lens 2.
	assert pnglens.EDITIONS["13.1#index-opaque-black"] == {3} and not pnglens.EXTENSION["13.1#index-opaque-black"]
	verdicts = judged(record(("13.1#index-opaque-black", "WARN")))
	assert [f.applies for f in verdicts["2"].faults] == [False] and verdicts["2"].expectation == "display"
	assert [f.applies for f in verdicts["3"].faults] == [True] and verdicts["3"].expectation == "recover"
	assert pnglens.faults_in_words(verdicts["2"]) == ""
	# With the second edition's error (11.2.2#index-in-range: reject, editions 2) each lens sees its own text.
	verdicts = judged(record(("11.2.2#index-in-range", "ERR"), ("13.1#index-opaque-black", "WARN")))
	assert verdicts["2"].expectation == "must-reject" and not verdicts["2"].conforms
	assert verdicts["3"].expectation == "recover"
	assert [f.id for f in verdicts["2"].faults if f.applies] == ["11.2.2#index-in-range"]
	assert [f.id for f in verdicts["3"].faults if f.applies] == ["13.1#index-opaque-black"]


def test_faults_in_words():
	d = Diagnostics()
	d.record("11.3.3.3#zlib", "WARN", "zTXt: bad stream; ignored", cause="rfc1951-3.2.3#btype-reserved")
	d.record("11.2.2#index-in-range", "ERR", "3 pixels index past the 2-entry palette")
	words = pnglens.faults_in_words(pnglens.verdict(d, pnglens.LENSES["3"])).split("\n")
	assert words == ["WARN [11.3.3.3#zlib] " + pnglens.CLAUSES["11.3.3.3#zlib"]["statement"]
		+ " Found: [rfc1951-3.2.3#btype-reserved] zTXt: bad stream; ignored"], words
	# The cause is part of the message; the fault's own clause sets the expectation.
	assert pnglens.verdict(d, pnglens.LENSES["3"]).expectation == "recover"


def test_rating():
	rate = pnglens.rate
	assert rate(()) == "recommended"
	table = {("trns", "first"): "arguable", ("trns", "last"): "arguable",
		("crc", "dropped"): "recommended", ("crc", "ignored"): "arguable",
		("critical", "ignored"): "arguable",
		("plte", "first"): "arguable", ("plte", "last"): "arguable"}
	for choice, rating in table.items():
		assert rate((choice,)) == rating, choice
	assert rate((("crc", "dropped"), ("trns", "last"))) == "arguable"
	assert rate((("crc", "dropped"), ("crc", "dropped"))) == "recommended"
	for v in judged(record()).values():
		assert v.rate((("crc", "ignored"),)) == "arguable" and v.rate(()) == "recommended"
	for bad in ((("crc", "repaired"),), [("crc", "dropped")]):
		try:
			rate(bad)
		except AssertionError:
			pass
		else:
			raise AssertionError(f"rated {bad!r}")


def test_no_decoder():
	assert "refpng" not in sys.modules, "the lens tests run without the decoder"


def main():
	tests = [test_lenses, test_edition_only_text_clauses, test_clean, test_recover, test_either, test_reject,
		test_strongest_wins, test_subject, test_apng_extension, test_idot_strict_note, test_edition_3_only,
		test_faults_in_words, test_rating, test_no_decoder]
	for test in tests:
		test()
	print(f"{len(tests)} lens tests passed")


if __name__ == "__main__":
	main()
