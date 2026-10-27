"""report.json -> one self-contained, minified HTML page.

	uv run report.py [out/report.json] [out/report.html]

run.py calls write() itself; this entry point re-renders a saved report.
"""
import html
import json
import sys
from pathlib import Path

import minify_html
from markdown_it import MarkdownIt

import pnglens

REPO = "https://github.com/MrMetric/difftest-png"
README = Path(__file__).with_name("README.md")
# glyph per outcome; colour comes from severity
GLYPH = {"pixels": "✓", "reject": "R", "wrong": "≠", "accept": "A", "crash": "☠", "memory": "M",
	"timeout": "⧗", "oom": "∞", "shim": "S"}
OUTCOME_TEXT = {
	"pixels": "decoded to the pixels of one of the reference's interpretations (validators: accepted)",
	"reject": "rejected the file",
	"wrong": "decoded to pixels no interpretation has",
	"accept": "decoded a file the lens says to reject, or one with no reference pixels",
	"crash": "crashed",
	"memory": "memory error under valgrind memcheck",
	"timeout": "timed out",
	"oom": "ran out of memory",
	"shim": "the wrapper could not use the library's output",
}
# an output whose label rates arguable is marked with a letter per topic of its arguable choices
TOPIC = {"trns": ("t", "one of several tRNS chunks used"), "crc": ("c", "a chunk with a bad CRC used as stored"),
	"plte": ("p", "one of several PLTE chunks used"), "critical": ("k", "an unknown critical chunk ignored")}
assert {topic for topic, _ in pnglens.RATINGS} == set(TOPIC)
CSS = """
body{margin:1rem;font:1rem/1.5 system-ui,sans-serif;color:#ddd;background:#151515}
a{color:#8bbcff}
table{border-collapse:collapse}
td,th{padding:.15rem .4rem;border:1px solid #444}
.s td{white-space:nowrap}
th{font-weight:normal;text-align:left}
.m td{text-align:center;padding:0;min-width:1.6rem}
.m td:first-child{text-align:left;padding:0 .4rem}
.m thead th{position:sticky;top:0;background:#151515;vertical-align:bottom;padding:.3rem .1rem}
.m thead th span{writing-mode:vertical-rl;transform:rotate(180deg);white-space:nowrap}
.m tr.sec th{background:#222;font-weight:bold}
summary{cursor:pointer}
details div{max-width:60rem;padding:.3rem 0 .5rem 1rem;color:#bbb}
.pass{color:#7c7}.should{color:#eb5;background:#332a10}.must{color:#f66;background:#3a1515}
.fatal{color:#fff;background:#a0206a}
.note{color:#eb5}
code{font-size:.9em}
body:has(#f:checked) tr.ok{display:none}
"""


def esc(s) -> str:
	return html.escape(str(s), quote=True)


def column_groups(report: dict) -> list[list[dict]]:
	"""Variants of one project whose results agree on every test share a column."""
	def key(impl):
		rs = report["results"][impl["name"]]
		return impl["project"], json.dumps({t: {k: v for k, v in r.items() if k != "detail"} for t, r in rs.items()}, sort_keys=True)
	groups: dict[tuple, list[dict]] = {}
	for impl in report["implementations"]:
		groups.setdefault(key(impl), []).append(impl)
	def order(g):
		s = g[0]["summary"]["3"]
		return s["must"], s["should"], g[0]["name"]
	return sorted(groups.values(), key=order)


def label(impl: dict) -> str:
	v = impl["version"] or ""
	if impl["revision"]:
		v = f"{impl['revision']}"
	return f"{impl['name']} {v}".strip() if impl["variant"] == "release" or not v else f"{impl['name']} @ {v}"


def marks(r: dict) -> str:
	"""Letters of the topics whose choice in an arguable label rates arguable, in TOPIC's order."""
	if r.get("rating") != "arguable":
		return ""
	chosen = {tuple(c.split(":")) for choices in r["label"]["choices"] if choices != "none" for c in choices.split(",")}
	topics = {topic for topic, option in chosen if pnglens.RATINGS[(topic, option)] == "arguable"}
	return "".join(TOPIC[t][0] for t in TOPIC if t in topics)


def cell(test: dict, r: dict) -> str:
	"""One matrix cell: the lens-3 outcome's glyph, coloured by the lens-3 severity; a memory error shows as M
	whatever the pixels."""
	o = r["outcome"] if isinstance(r["outcome"], str) else r["outcome"]["3"]
	cls = "fatal" if o in ("crash", "timeout", "oom") or "memory" in r else r["severity"]["3"]
	mark = marks(r) if "memory" not in r else ""
	return f"<td class={cls}>{GLYPH['memory' if 'memory' in r else o]}{f'<sup>{mark}</sup>' if mark else ''}</td>"


def expectation_text(test: dict) -> str:
	parts = {lens: f"{test['expectation'][lens]}, {'conforms' if test['conforms'][lens] else 'does not conform'}" for lens in test["expectation"]}
	text = next(iter(parts.values())) if len(set(parts.values())) == 1 else "; ".join(f"lens {lens}: {p}" for lens, p in parts.items())
	return text + (f"; strict: {', '.join(test['strict'])}" if test["strict"] else "")


def disclaimer() -> str:
	"""The README's "Human disclaimer" section, verbatim, rendered as CommonMark; the README is its only copy."""
	head = "\n## Human disclaimer\n\n"
	text = README.read_text(encoding="utf-8")
	assert text.count(head) == 1, f"{README} must have exactly one '## Human disclaimer' section"
	body = text.split(head, 1)[1].split("\n## ", 1)[0].strip()
	assert body, f"{README}: the '## Human disclaimer' section is empty"
	return "<h2>Human disclaimer</h2>" + MarkdownIt("commonmark").render(body)


def render(report: dict) -> str:
	groups = column_groups(report)
	tests = report["tests"]
	date = report["generated"][:10]
	out = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
		f"<title>PNG decoder conformance, {date}</title><style>{CSS}</style>"]
	out.append(f"""<h1>PNG decoder conformance</h1>
<p>Every decoder below was run over the same {len(tests)} small PNG files from <a href="{REPO}">difftest-png</a>, each one built to test one rule of the PNG specification or one behaviour on which some decoder deviates. Run {esc(report['generated'])} on {esc(report['host'])}.</p>
<p>Each result is rated under each lens: <b>pass</b>, <b class=should>should</b> (the specification says a decoder should do otherwise, e.g. recover a recoverable fault instead of rejecting the image) or <b class=must>must</b> (a requirement is broken: wrong pixels, an invalid file accepted, a valid file rejected, a crash, a hang, a memory error). Warnings never count against a decoder. Running out of memory counts like a crash: a file should be rejected with an error, and a decoder that instead exhausts its host hands every program that opens untrusted images a denial of service. Pixels are compared as 16-bit RGBA with no colour management; colour under fully transparent pixels and the 8-bit reduction of 16-bit images are recorded as properties, not failures. Validators (pngcheck) are judged on a different question: whether each file conforms to the specification. Some files a decoder must still display do not conform, for example one with a chunk name the specification reserves.</p>
<p>Two editions of the specification matter: the second (ISO/IEC 15948:2003; PNG 1.0 and 1.1 count as the second) and the third (W3C, 2025). A lens is an edition's text plus the extensions: the W3C registered extensions, Apple's iDOT, and under the second edition the chunks the third edition added (APNG, cICP, mDCV, cLLI, eXIf). Every result is rated through both lenses separately, and the summary shows both side by side; the matrix shows the third edition's. There is no single verdict: following one edition rather than the other is not a defect. The two ratings differ only where the texts contradict each other, which they do in one place: a palette index past the end of PLTE is an error in the second edition and renders opaque black in the third.</p>
<p>Not tested: animation frames after the default image, colour management (gAMA, cHRM, iCCP, sRGB, cICP, sBIT), encoders, performance. Passing every test means the suite is incomplete, not that the decoder is perfect.</p>""")
	if report["problems"]:
		out.append("<p class=must>Suite consistency problems in this run:</p><ul>" + "".join(f"<li>{esc(p)}" for p in report["problems"]) + "</ul>")

	out.append(disclaimer())
	lenses = report["lenses"]
	heads = ("implementation", "version", "lineage", *(f"{k} (lens {lens})" for lens in lenses for k in ("must", "should", "silent")), "alpha-0 colour", "16→8")
	out.append("<h2>Summary</h2><table class=s><tr>" + "".join(f"<th>{h}</th>" for h in heads) + "</tr>")
	for g in groups:
		s = g[0]["summary"]
		names = "<br>".join(esc(i["name"]) for i in g)
		vers = "<br>".join(esc(i["revision"] or i["version"] or "") for i in g)
		a0 = ", ".join(s.get("alpha0", []))
		a0cls = " class=should" if "altered" in s.get("alpha0", []) else ""
		# silent accepts of non-conforming files are shown, not judged: no severity colour
		counts = "".join(f"<td>{s[lens][k]}</td>" if k == "silent" else f"<td class={k if s[lens][k] else 'pass'}>{s[lens][k]}</td>"
			for lens in lenses for k in ("must", "should", "silent"))
		out.append(f"<tr><td title='{esc(g[0]['notes'])}'>{names}</td><td>{vers}</td><td>{esc(g[0]['lineage'])}</td>"
			f"{counts}<td{a0cls}>{esc(a0)}</td><td>{esc(', '.join(s.get('16to8', [])))}</td></tr>")
	out.append("</table>")

	out.append("<h2 id=results>Results</h2><p><label><input type=checkbox id=f> show only tests some implementation fails</label> · "
		+ " ".join(f"<span>{g} {esc(t.split(' (')[0])}</span> ·" for t, g in ((OUTCOME_TEXT[k], v) for k, v in GLYPH.items())).rstrip(" ·")
		+ "".join(f" · <span>✓<sup>{m}</sup> {esc(t)} (arguable)</span>" for m, t in TOPIC.values()) + "</p>")
	out.append("<table class=m><thead><tr><th><span>test</span></th>" + "".join(
		f"<th title='{esc(chr(10).join(label(i) for i in g))}'><span>{esc(' · '.join(i['name'] for i in g))}</span></th>" for g in groups) + "</tr></thead><tbody>")
	section = None
	for t in sorted(tests, key=lambda t: (section_key(t["clause"]), t["id"])):
		sec = t["clause"].split("#")[0].split(".")[0] if t["clause"] else "?"
		if sec != section:
			section = sec
			out.append(f"<tr class=sec><th colspan={len(groups) + 1}>{esc(SECTION_NAMES.get(sec, 'Section ' + sec))}</th></tr>")
		cells = [cell(t, report["results"][g[0]["name"]][t["id"]]) for g in groups]
		ok = all(sev == "pass" for g in groups for sev in report["results"][g[0]["name"]][t["id"]]["severity"].values())
		stmt = f"<br><b>{esc(t['clause'])}</b>: {esc(t['statement'])}" if t["statement"] else f"<br><b>{esc(t['clause'])}</b>"
		out.append(f"<tr{' class=ok' if ok else ''}><td><details><summary>{esc(t['id'])}</summary><div>{esc(t['description'])}{stmt}"
			f"<br>expected: {esc(expectation_text(t))}<br><a href='{REPO}/blob/HEAD/decode/{esc(t['file'])}'>{esc(t['file'])}</a></div></details></td>{''.join(cells)}</tr>")
	out.append("</tbody></table>")
	return "".join(out)


SECTION_NAMES = {
	"4": "4 Concepts (APNG)", "5": "5 Datastream structure", "6": "6 Reference image to PNG image transformation", "7": "7 Encoding the PNG image as a PNG datastream",
	"8": "8 Interlacing and pass extraction", "9": "9 Filtering", "10": "10 Compression", "11": "11 Chunk specifications",
	"12": "12 PNG encoders", "13": "13 PNG decoders and viewers", "15": "15 Conformance", "rfc1950": "RFC 1950 zlib", "rfc1951": "RFC 1951 DEFLATE",
}


def section_key(clause: str) -> tuple:
	head = clause.split("#")[0] if clause else "~"
	parts = head.replace("rfc1950-", "100.").replace("rfc1951-", "101.").split(".")
	return tuple(int(p) if p.isdigit() else 999 for p in parts)


def write(report: dict, path: Path) -> None:
	# render() closes every element: minify-html (0.16 to 0.18 at least) drops a </table> that follows an
	# implicitly closed cell, which pulls the rest of the page into the table.
	path.write_text(minify_html.minify(render(report), minify_css=True, keep_closing_tags=False))


if __name__ == "__main__":
	src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("out/report.json")
	dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".html")
	write(json.loads(src.read_text()), dst)
