"""report.json -> one self-contained, minified HTML page.

	uv run report.py [out/report.json] [out/report.html]

run.py calls write() itself; this entry point re-renders a saved report from the JSON alone.
The page reads completely without JavaScript: the lens switch and the failing-only filter are CSS. The
script (SCRIPT) adds column sorting, a column picker and row filters; its controls are `hidden` until it runs.
"""
import html
import json
import re
import sys
from pathlib import Path

import minify_html
from markdown_it import MarkdownIt


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
# a reference in a clause's `says`: `§` and a section number, optionally after a document name; its sections.tsv
# key is the reference without the `§` (references())
REF = re.compile(r"(?:(?:2nd ed\.|RFC 195[01]|Extensions(?: 1\.5\.0)?|GIF89a) )?§\d+(?:\.\d+)*(?: Table \d+)?")
SEVERITY_STYLE = {"pass": "color:#7c7", "should": "color:#eb5;background:#332a10", "must": "color:#f66;background:#3a1515",
	"fatal": "color:#fff;background:#a0206a"}
SEVERITY_TEXT = {"pass": "pass", "should": "should-violation", "must": "must-violation",
	"fatal": "crash, hang, out of memory or memory error (a must-violation)"}
THUMB_PX = 60  # the height of an output's thumbnail
CSS = """
body{margin:1rem;font:1rem/1.5 system-ui,sans-serif;color:#ddd;background:#151515}
a{color:#8bbcff}
table{border-collapse:collapse}
td,th{padding:.15rem .4rem;border:1px solid #444}
.s td{white-space:nowrap}
th{font-weight:normal;text-align:left}
.m td{text-align:center;padding:0;min-width:1.6rem;color:#7c7}
.m td:first-child{text-align:left;padding:0 .4rem;color:inherit}
.m thead th{position:sticky;top:0;background:#151515;vertical-align:bottom;padding:.3rem .1rem}
.m thead th span{writing-mode:vertical-rl;transform:rotate(180deg);white-space:nowrap}
.f tbody th{white-space:nowrap}
summary{cursor:pointer}
#mx summary{display:flex;gap:.4rem;list-style:none}
#mx summary::before{content:"▸"}
#mx details[open]>summary::before{content:"▾"}
#mx summary a{margin-left:auto}
#mx td{vertical-align:top}
#mx details[open]{width:48rem}
#feat button{all:unset;color:#8bbcff;cursor:pointer}
.x{max-width:60rem;padding:.3rem 0 .5rem 1rem;color:#bbb}
.x p{margin:.3rem 0}
.cs{display:flex;flex-wrap:wrap;gap:2px}
.cs b{font:600 .8rem/1.5 ui-monospace,monospace;padding:0 .3rem;border:2px solid transparent;color:#eee}
.ihdr{background:#2b5c9e}.plte{background:#7a3d9e}.idat{background:#2e7d4f}.iend{background:#4f5b6b}
.anc{background:#3a3a3a}.crit{background:#a5641c}.invalid{background:#8a1d4a}.sig{background:#b01c1c}
.trailer{background:#222;border-style:dashed!important;border-color:#777!important}
.cs .bad{border-color:#f44}.cs .cut{border-right:6px solid #f44}.cut::after{content:" …"}.cs .dim{opacity:.4}
.outs{display:flex;flex-wrap:wrap;gap:.8rem;align-items:flex-start;font-size:.85rem}
.outs div{display:flex;flex-direction:column;gap:.2rem;max-width:16rem}
.t{display:block;height:60px;image-rendering:pixelated;background:var(--i) 0 0/100% 100%,repeating-conic-gradient(#777 0 25%,#555 0 50%) 0 0/8px 8px}
.big{display:block;height:60px;line-height:60px}
.legend td:first-child{text-align:center}
.note{color:#ddd}
code{font-size:.9em}
""" + "".join(f".{cls}{{{style}}}" for cls, style in SEVERITY_STYLE.items())


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


def marks(r: dict, ratings: dict[str, str]) -> str:
	"""Letters of the topics whose choice in an arguable label rates arguable, in TOPIC's order. `ratings`:
	report.json's choice -> rating."""
	if r.get("rating") != "arguable":
		return ""
	chosen = {c for choices in r["label"]["choices"] if choices != "none" for c in choices.split(",")}
	topics = {c.split(":")[0] for c in chosen if ratings[c] == "arguable"}
	return "".join(TOPIC[t][0] for t in TOPIC if t in topics)


def expectation_text(test: dict) -> str:
	parts = {lens: f"{test['expectation'][lens]}, {'conforms' if test['conforms'][lens] else 'does not conform'}" for lens in test["expectation"]}
	text = next(iter(parts.values())) if len(set(parts.values())) == 1 else "; ".join(f"lens {lens}: {p}" for lens, p in parts.items())
	return text + (f"; strict: {', '.join(test['strict'])}" if test["strict"] else "")


def outcome(r: dict, lens: str) -> str:
	return r["outcome"] if isinstance(r["outcome"], str) else r["outcome"][lens]


def lens_td(per_lens: dict[str, tuple[str, str]], extra: str = "") -> str:
	"""A cell whose class and text depend on the lens: per_lens is lens -> (class, text). When the lenses agree the
	text is plain; otherwise each lens's class carries its digit and its text sits in data-<lens>, which the
	selected lens's CSS shows (lens_css()). `extra` is lens-independent HTML after the text."""
	if len(set(per_lens.values())) == 1:
		cls, text = next(iter(per_lens.values()))
		return f"<td{f' class={cls}' if cls else ''}>{esc(text)}{extra}</td>"
	classes = " ".join(f"{cls}{lens}" for lens, (cls, _) in per_lens.items() if cls)
	data = "".join(f" data-{lens}='{esc(text)}'" for lens, (_, text) in per_lens.items())
	return f"<td{f' class={chr(39)}{classes}{chr(39)}' if classes else ''}{data}>{extra}</td>"


def lens_css(lenses: list[str]) -> str:
	"""The lens switch: the checked radio #l<lens> selects which data-<lens> text and <class><lens> colour show."""
	out = []
	for lens in lenses:
		on = f"body:has(#l{lens}:checked)"
		out.append(f"{on} [data-{lens}]::before{{content:attr(data-{lens})}}")
		out += [f"{on} .{cls}{lens}{{{style}}}" for cls, style in SEVERITY_STYLE.items()]
		out.append(f"body:has(#f:checked):has(#l{lens}:checked) tr.ok{lens}{{display:none}}")
	return "".join(out)


def cell(r: dict, lenses: list[str], ratings: dict[str, str]) -> str:
	"""One matrix cell: under each lens, the outcome's glyph coloured by its severity (no class for a pass, the
	column's default); a crash, hang or out-of-memory is `fatal`, and a memory error shows as M whatever the
	pixels. The arguable marks are lens-independent."""
	mark = marks(r, ratings) if "memory" not in r else ""
	sup = f"<sup>{mark}</sup>" if mark else ""
	if "memory" in r:
		return f"<td class=fatal>{GLYPH['memory']}{sup}</td>"
	per = {}
	for lens in lenses:
		o = outcome(r, lens)
		cls = "fatal" if o in ("crash", "timeout", "oom") else r["severity"][lens]
		per[lens] = ("" if cls == "pass" else cls, GLYPH[o])
	return lens_td(per, sup)


def violation(test: dict, impl: dict, r: dict, lens: str) -> str | None:
	"""The SUMMARY cell one result counts in under a lens, or None when it passes. For a decoder, with E the
	lens's expectation for the file:
	- `rejected` (valid file rejected): outcome reject with E display.
	- `accepted` (invalid file accepted): outcome accept with E must-reject.
	- `wrong` (wrong pixels): outcome wrong, E display, recover or may-reject.
	- `fatal`: outcome crash, timeout, oom or shim, any E; or a memory error under valgrind, any outcome.
	- `should` (recoverable file rejected): outcome reject with E recover.
	A validator is judged on whether the file conforms: `rejected` is a reject of a conforming file that
	carries no strict note, `accepted` an accept (outcome pixels) of a file that does not conform, and
	`fatal` as for a decoder. Every must-violation is in exactly one of the first four."""
	o, sev, e = outcome(r, lens), r["severity"][lens], test["expectation"][lens]
	if "memory" in r or o in ("crash", "timeout", "oom", "shim"):
		assert sev == "must", (test["id"], impl["name"], r)
		return "fatal"
	if sev == "pass":
		return None
	if impl["pixels"]:
		kind = {("reject", "display"): "rejected", ("reject", "recover"): "should", ("accept", "must-reject"): "accepted"}.get((o, e))
		kind = kind or ("wrong" if o == "wrong" and e != "must-reject" else None)
	else:
		kind = {("reject", True): "rejected", ("pixels", False): "accepted"}.get((o, test["conforms"][lens]))
	assert kind and (sev == "should") == (kind == "should"), (test["id"], impl["name"], lens, o, sev, e)
	return kind


SUMMARY = {"rejected": "valid file rejected", "accepted": "invalid file accepted", "wrong": "wrong pixels",
	"fatal": "crash, hang, out of memory, memory error or shim failure", "should": "recoverable file rejected"}


def counts(report: dict, impl: dict) -> dict[str, dict[str, int]]:
	"""lens -> SUMMARY key -> how many tests the implementation's results count in it (violation())."""
	out = {}
	for lens in report["lenses"]:
		c = dict.fromkeys(SUMMARY, 0)
		for t in report["tests"]:
			kind = violation(t, impl, report["results"][impl["name"]][t["id"]], lens)
			if kind:
				c[kind] += 1
		s = impl["summary"][lens]
		assert sum(c[k] for k in SUMMARY if k != "should") == s["must"] and c["should"] == s["should"], (impl["name"], lens, c, s)
		out[lens] = c
	return out


def references(text: str) -> list[str]:
	"""The sections.tsv keys of the references in a `says` text, in order: a reference is `§` and a section
	number, optionally after a document name (REF), and its key is the reference without the `§`."""
	return [m.group(0).replace("§", "") for m in REF.finditer(text)]


def linked(text: str, sections: dict) -> str:
	"""The text, escaped, with each reference (REF) linked to its section's url, or plain when it has none."""
	out, at = [], 0
	for m in REF.finditer(text):
		s = sections[m.group(0).replace("§", "")]
		ref = esc(m.group(0))
		out += [esc(text[at:m.start()]), f"<a href='{esc(s['url'])}'>{ref}</a>" if s["url"] else ref]
		at = m.end()
	return "".join(out) + esc(text[at:])


def chunk_class(ctype: str) -> str:
	if ctype in ("IHDR", "PLTE", "IDAT", "IEND"):
		return ctype.lower()
	if not (len(ctype) == 4 and ctype.isascii() and ctype.isalpha()):
		return "invalid"
	return "anc" if ctype[0].islower() else "crit"


def strip(walk: dict) -> str:
	"""The chunk strip: a box per chunk, a run of chunks with the same type and CRC status collapsed into one
	box with ×N; a bad CRC outlined red, a truncated chunk with a red right edge, chunks after the first IEND
	dimmed; a wrong signature and a trailer of bytes that hold no chunk header are boxes of their own."""
	boxes = [] if walk["signature"] else ["<b class=sig>signature</b>"]
	runs: list[list] = []
	after = False
	for c in walk["chunks"]:
		key = (c["type"], c["crc"], c["truncated"], after)
		if runs and runs[-1][0] == key:
			runs[-1][1] += 1
		else:
			runs.append([key, 1])
		after = after or c["type"] == "IEND"
	for (ctype, crc, truncated, dim), n in runs:
		cls = [chunk_class(ctype)] + (["bad"] if crc == "bad" else []) + (["cut"] if truncated else []) + (["dim"] if dim else [])
		boxes.append(f"<b class='{' '.join(cls)}'>{esc(ctype)}{f' ×{n}' if n > 1 else ''}</b>")
	if walk["trailer"]:
		boxes.append(f"<b class=trailer>+{walk['trailer']} bytes</b>")
	return "<div class=cs>" + "".join(boxes) + "</div>"


def thumb(digest: str, images: dict, classes: dict[str, str]) -> str:
	"""An image at THUMB_PX high (its CSS class holds the PNG), or its size when report.json has no PNG of it."""
	im = images[digest]
	if "png" not in im:
		return f"<span class=big>{im['width']}×{im['height']}</span>"
	return f"<i class='t {classes[digest]}'></i>"


def thumb_css(images: dict, classes: dict[str, str]) -> str:
	out = []
	for digest, cls in classes.items():
		im = images[digest]
		out.append(f".{cls}{{width:{max(1, round(THUMB_PX * im['width'] / im['height']))}px;--i:url(data:image/png;base64,{im['png']})}}")
	return "".join(out)


def bucket_label(b: dict, r: dict, ratings: dict[str, str]) -> str:
	"""Which interpretations a bucket matches, with the topics of an arguable rating and the match's properties;
	`no interpretation` and how it differs when it matches none."""
	tag = b["label"]
	if tag == "unexplained":
		return f"no interpretation: {r['mismatch']}"
	if tag == "unverifiable":
		return "no reference pixels"
	# `none` is the interpretation of a file with no open decision
	text = "matches " + (" or ".join(c for c in tag["choices"] if c != "none") if tag["choices"] != ["none"] else "the reference")
	mark = marks(r, ratings)
	if mark:
		text += " (arguable: " + "; ".join(TOPIC[t][1] for t, (m, _) in TOPIC.items() if m in mark) + ")"
	if "alpha0" in tag:
		text += f"; alpha-0 colour {tag['alpha0']}"
	if "16to8" in tag:
		text += f"; 16→8 {tag['16to8']}"
	return text


def expansion(test: dict, report: dict, classes: dict[str, str]) -> str:
	"""A test's details: chunk strip, what the file is (note, then each clause's `says`), the expectation, the
	sections not already linked, and the outputs: the expected image, then each bucket with its columns."""
	ids = test["about"] or ([test["clause"]] if test["clause"] else [])
	clauses = [report["clauses"][c] for c in ids]
	sections = report["sections"]
	says = [c["says"] for c in clauses if c["says"]]
	done = {k for s in says for k in references(s)}
	refs = []
	for c in clauses:
		for tok in c["section"]:
			if tok not in done:
				done.add(tok)
				s = sections[tok]
				refs.append(f"<a href='{esc(s['url'])}'>{esc(s['label'])}</a>" if s["url"] else esc(s["label"]))
	note = test["note"] + ("" if test["note"].endswith(".") else ".")  # a note is a caption; the page punctuates it
	out = [strip(test["chunks"]), f"<p><span class=note>{esc(note)}</span>" + "".join(" " + linked(s, sections) for s in says) + "</p>",
		f"<p>expected: {esc(expectation_text(test))}</p>"]
	if refs:
		out.append("<p>spec: " + ", ".join(refs) + "</p>")
	outs = []
	if test["preferred"]:
		outs.append(f"<div>{thumb(test['preferred'], report['images'], classes)}<span>expected</span></div>")
	for b in test["buckets"]:
		r = report["results"][b["columns"][0]][test["id"]]
		outs.append(f"<div>{thumb(b['digest'], report['images'], classes)}<span>{esc(bucket_label(b, r, report['ratings']))}</span>"
			f"<span>{b['count']}: {esc(', '.join(b['columns']))}</span></div>")
	if outs:
		out.append("<div class=outs>" + "".join(outs) + "</div>")
	return "<div class=x>" + "".join(out) + "</div>"


def legend() -> str:
	rows = [f"<tr><td>{g}</td><td></td><td>{esc(OUTCOME_TEXT[k])}</td></tr>" for k, g in GLYPH.items()]
	rows += [f"<tr><td>✓<sup>{m}</sup></td><td></td><td>{esc(t)} (arguable)</td></tr>" for m, t in TOPIC.values()]
	rows += [f"<tr><td></td><td class={cls}>{cls}</td><td>{esc(text)}</td></tr>" for cls, text in SEVERITY_TEXT.items()]
	return "<h2>Legend</h2><table class=legend><tr><th>glyph</th><th>colour</th><th>meaning</th></tr>" + "".join(rows) + "</table>"


def families(report: dict) -> list[str]:
	return list(dict.fromkeys(t["family"] for t in report["tests"]))


def feature_table(report: dict, groups: list[list[dict]], heads: str) -> str:
	"""Per family and column group: n/m, the n of the family's m tests the group fails (must or should) under
	the lens, coloured by the worst severity; a pass glyph when n is 0."""
	lenses = report["lenses"]
	out = ["<h2>By family</h2><table class='m f' id=feat><thead><tr><th><span>family</span></th>" + heads + "</tr></thead><tbody>"]
	for fam in families(report):
		tests = [t for t in report["tests"] if t["family"] == fam]
		cells = []
		for g in groups:
			rs = report["results"][g[0]["name"]]
			per = {}
			for lens in lenses:
				sev = [rs[t["id"]]["severity"][lens] for t in tests]
				n = sum(s != "pass" for s in sev)
				per[lens] = ("must" if "must" in sev else "should", f"{n}/{len(tests)}") if n else ("pass", "✓")
			cells.append(lens_td(per))
		out.append(f"<tr><th><span class=fam>{esc(fam)}</span></th>{''.join(cells)}</tr>")
	return "".join(out) + "</tbody></table>"


def summary_table(report: dict, groups: list[list[dict]]) -> str:
	lenses = report["lenses"]
	heads = ("implementation", "version", "lineage", *(f"must: {SUMMARY[k]}" for k in SUMMARY if k != "should"), f"should: {SUMMARY['should']}",
		"silent", "strict", "alpha-0 colour", "16→8")
	order = {i["name"]: n for n, i in enumerate(report["implementations"])}
	out = ["<h2>Summary</h2><table class=s id=sum><thead><tr>" + "".join(f"<th>{h}</th>" for h in heads) + "</tr></thead><tbody>"]
	for g in groups:
		s = g[0]["summary"]
		c = counts(report, g[0])
		names = "<br>".join(esc(i["name"]) for i in g)
		vers = "<br>".join(esc(i["revision"] or i["version"] or "") for i in g)
		a0 = ", ".join(s.get("alpha0", []))
		a0cls = " class=should" if "altered" in s.get("alpha0", []) else ""
		cells = [lens_td({lens: ((k if k == "should" else "must") if c[lens][k] else "pass", str(c[lens][k])) for lens in lenses}) for k in SUMMARY]
		# silent accepts of non-conforming files are shown, not judged: no severity colour
		cells.append(lens_td({lens: ("", str(s[lens]["silent"])) for lens in lenses}))
		sort = "".join(f" data-m{lens}={s[lens]['must']} data-s{lens}={s[lens]['should']}" for lens in lenses)
		out.append(f"<tr{sort} data-o={order[g[0]['name']]}><td title='{esc(g[0]['notes'])}'>{names}</td><td>{vers}</td><td>{esc(g[0]['lineage'])}</td>"
			f"{''.join(cells)}<td>{s.get('strict', '')}</td><td{a0cls}>{esc(a0)}</td><td>{esc(', '.join(s.get('16to8', [])))}</td></tr>")
	return "".join(out) + "</tbody></table>"


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
	lenses = report["lenses"]
	date = report["generated"][:10]
	assert {c.split(":")[0] for c in report["ratings"]} == set(TOPIC), "a choice topic TOPIC has no letter for"
	digests = sorted({d for t in tests for d in [*(b["digest"] for b in t["buckets"]), t["preferred"]] if d and "png" in report["images"][d]})
	classes = {d: f"i{n}" for n, d in enumerate(digests)}
	out = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
		f"<title>PNG decoder conformance, {date}</title><style>{CSS}{lens_css(lenses)}{thumb_css(report['images'], classes)}</style>"]
	out.append(f"""<h1>PNG decoder conformance</h1>
<p>Every decoder below was run over the same {len(tests)} small PNG files from <a href="{REPO}">difftest-png</a>, each one built to test one rule of the PNG specification or one behaviour on which some decoder deviates. Run {esc(report['generated'])} on {esc(report['host'])}.</p>
<p>Each result is rated under each lens: <b>pass</b>, <b class=should>should</b> (the specification says a decoder should do otherwise, e.g. recover a recoverable fault instead of rejecting the image) or <b class=must>must</b> (a requirement is broken: wrong pixels, an invalid file accepted, a valid file rejected, a crash, a hang, a memory error). Warnings never count against a decoder. Running out of memory counts like a crash: a file should be rejected with an error, and a decoder that instead exhausts its host hands every program that opens untrusted images a denial of service. Pixels are compared as 16-bit RGBA with no colour management; colour under fully transparent pixels and the 8-bit reduction of 16-bit images are recorded as properties, not failures. Validators (pngcheck) are judged on a different question: whether each file conforms to the specification. Some files a decoder must still display do not conform, for example one with a chunk name the specification reserves.</p>
<p>Two editions of the specification matter: the second (ISO/IEC 15948:2003; PNG 1.0 and 1.1 count as the second) and the third (W3C, 2025). A lens is an edition's text plus the extensions: the W3C registered extensions, Apple's iDOT, and under the second edition the chunks the third edition added (APNG, cICP, mDCV, cLLI, eXIf). Every result is rated through both lenses separately; the lens switch below chooses which one every table shows. There is no single verdict: following one edition rather than the other is not a defect. The two ratings differ only where the texts contradict each other, which they do in one place: a palette index past the end of PLTE is an error in the second edition and renders opaque black in the third.</p>
<p>Not tested: animation frames after the default image, colour management (gAMA, cHRM, iCCP, sRGB, cICP, sBIT), encoders, performance. Passing every test means the suite is incomplete, not that the decoder is perfect.</p>""")
	if report["problems"]:
		out.append("<p class=must>Suite consistency problems in this run:</p><ul>" + "".join(f"<li>{esc(p)}" for p in report["problems"]) + "</ul>")

	out.append(disclaimer())
	assert "3" in lenses, "lens 3 is the one selected by default"
	out.append("<p class=lens>lens: " + " ".join(f"<label><input type=radio name=lens id=l{lens} value={lens}{' checked' if lens == '3' else ''}> {lens}</label>"
		for lens in lenses) + "</p>")
	out.append(legend())
	heads = "".join(f"<th title='{esc(chr(10).join(label(i) for i in g))}'><span>{esc(' · '.join(i['name'] for i in g))}</span></th>" for g in groups)
	out.append("<p class=js hidden><label>sort columns <select id=sort><option value=v>by violations<option value=n>by name"
		"<option value=o>in impls.toml order</select></label></p><details class=js hidden><summary>columns</summary>"
		+ "".join(f"<label><input type=checkbox checked data-g={n}> {esc(' · '.join(i['name'] for i in g))}</label> " for n, g in enumerate(groups))
		+ "</details>")
	out.append(summary_table(report, groups))
	out.append(feature_table(report, groups, heads))
	out.append("<h2 id=results>Results</h2><p><label><input type=checkbox id=f> show only tests some implementation fails</label></p>"
		"<p class=js hidden><input id=q type=search placeholder='id, note or family'> "
		+ " ".join(f"<label><input type=checkbox checked data-e={e}> {e}</label>" for e in report["expectations"][::-1])
		+ " <select id=fam><option value=''>every family" + "".join(f"<option>{esc(f)}" for f in families(report)) + "</select></p>")
	out.append(f"<table class=m id=mx><thead><tr><th><span>test</span></th>{heads}</tr></thead><tbody>")
	for t in tests:
		rs = [report["results"][g[0]["name"]][t["id"]] for g in groups]
		ok = "".join(f" ok{lens}" for lens in lenses if all(r["severity"][lens] == "pass" for r in rs)).strip()
		data = "".join(f" data-e{lens}={t['expectation'][lens]}" for lens in lenses)
		out.append(f"<tr{f' class={chr(39)}{ok}{chr(39)}' if ok else ''}{data} data-f='{esc(t['family'])}'><td><details><summary><span>{esc(t['id'])}</span>"
			f"<a href='{REPO}/blob/HEAD/decode/{esc(t['file'])}'>png</a></summary>{expansion(t, report, classes)}</details></td>"
			f"{''.join(cell(r, lenses, report['ratings']) for r in rs)}</tr>")
	out.append("</tbody></table>")
	out.append(f"<script>{SCRIPT}</script>")
	return "".join(out)


SCRIPT = """
const $ = (s, e = document) => [...e.querySelectorAll(s)];
const byId = id => document.getElementById(id);
for (const e of $(".js")) e.hidden = false;
const lens = () => $("[name=lens]").find(e => e.checked).value;
const sumRows = $("#sum tbody tr");
const cellsOf = new Map([...$("#feat tr"), ...$("#mx tr")].map(r => [r, [...r.children].slice(1)]));
const groups = sumRows.map((tr, i) => ({i, tr, name: tr.cells[0].firstChild.textContent, o: +tr.dataset.o}));
const cmp = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const sortBy = byId("sort");
function sort() {
	const L = lens(), d = g => g.tr.dataset;
	const key = {
		v: (a, b) => d(a)["m" + L] - d(b)["m" + L] || d(a)["s" + L] - d(b)["s" + L] || cmp(a.name, b.name),
		n: (a, b) => cmp(a.name, b.name),
		o: (a, b) => a.o - b.o,
	}[sortBy.value];
	const order = [...groups].sort(key);
	for (const g of order) g.tr.parentNode.append(g.tr);
	for (const [r, cells] of cellsOf) for (const g of order) r.append(cells[g.i]);
}
const picks = $("[data-g]");
for (const c of picks) c.onchange = () => {
	const g = groups[c.dataset.g];
	g.tr.hidden = !c.checked;
	for (const cells of cellsOf.values()) cells[g.i].hidden = !c.checked;
};
const rows = $("#mx tbody tr");
const text = rows.map(r => [r.querySelector("summary span"), r.querySelector(".note")].map(e => e.textContent).join(" ").concat(" ", r.dataset.f).toLowerCase());
const q = byId("q"), fam = byId("fam"), exp = $("[data-e]");
function filter() {
	const L = lens(), s = q.value.toLowerCase(), on = new Set(exp.filter(e => e.checked).map(e => e.dataset.e));
	rows.forEach((r, i) => r.hidden = !(text[i].includes(s) && on.has(r.dataset["e" + L]) && (!fam.value || r.dataset.f == fam.value)));
}
for (const s of $(".fam")) {
	const b = document.createElement("button");
	b.textContent = s.textContent;
	b.onclick = () => { fam.value = s.textContent; filter(); byId("results").scrollIntoView(); };
	s.replaceWith(b);
}
for (const r of $("[name=lens]")) r.onchange = () => { if (sortBy.value == "v") sort(); filter(); };
sortBy.onchange = sort;
q.oninput = fam.onchange = filter;
for (const e of exp) e.onchange = filter;
sort();
filter();
for (const c of picks) c.onchange();
"""


def write(report: dict, path: Path) -> None:
	# render() closes every element: minify-html (0.16 to 0.18 at least) drops a </table> that follows an
	# implicitly closed cell, which pulls the rest of the page into the table.
	path.write_text(minify_html.minify(render(report), minify_css=True, keep_closing_tags=False))


if __name__ == "__main__":
	src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("out/report.json")
	dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".html")
	write(json.loads(src.read_text()), dst)
