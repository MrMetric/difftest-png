"""report.json -> one self-contained, minified HTML page.

	uv run report.py [out/report.json] [out/report.html]

run.py calls write() itself; this entry point re-renders a saved report from the JSON alone.
The page reads completely without JavaScript: the lens switch and the failing-only filter are CSS. The
script (SCRIPT) adds column sorting, a column picker, row filters and the implementations table's header sort; its
controls are `hidden` until it runs.
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
# glyph per outcome; color comes from severity
GLYPH = {"pixels": "✓", "reject": "R", "wrong": "≠", "accept": "A", "crash": "☠", "memory": "M",
	"timeout": "⧗", "oom": "∞", "api": "!", "shim": "-"}
# an output whose label rates arguable is marked with a letter per topic of its arguable choices
TOPIC = {"trns": ("t", "one of several tRNS chunks used"), "crc": ("c", "a chunk with a bad CRC used as stored"),
	"plte": ("p", "one of several PLTE chunks used"), "critical": ("k", "an unknown critical chunk ignored")}
# a reference in a clause's `says`: `§` and a section number, optionally after a document name; its sections.tsv
# key is the reference without the `§` (references())
REF = re.compile(r"(?:(?:2nd ed\.|RFC 195[01]|Extensions(?: 1\.5\.0)?|GIF89a) )?§\d+(?:\.\d+)*(?: Table \d+)?")
SEVERITY_STYLE = {"pass": "color:#7c7", "should": "color:#eb5;background:#332a10", "must": "color:#f66;background:#3a1515",
	"fatal": "color:#fff;background:#a0206a", "api": "color:#fff;background:#c0181c", "shim": "color:#aaa;background:#333"}
# What a cell's color counts as; the legend names it beside each cell it can show.
COUNTS_AS = {"pass": "pass", "should": "should-violation", "must": "must-violation", "fatal": "must-violation",
	"api": "must-violation", "shim": "not scored"}
# Every cell a reader can meet: outcome glyph in its severity's color, and what happened. Severity follows from
# the outcome except for a rejection (pass, should or must by what the lens expected) and an accept (pass for a
# file with no reference pixels, must for one the lens says to reject). legend() checks the results fit this list.
LEGEND = [
	("pixels", "pass", "decoded to the pixels of one of the reference's interpretations; a validator: accepted a conforming file"),
	("accept", "pass", "decoded a file that has no reference pixels, which the lens allows to be rejected"),
	("accept", "must", "decoded a file the lens says to reject; a validator: accepted a file that does not conform"),
	("reject", "pass", "rejected a file the lens allows to be rejected; a validator: rejected a file that does not conform"),
	("reject", "should", "rejected a file a decoder should recover and display"),
	("reject", "must", "rejected a valid file; a validator: rejected a conforming file"),
	("wrong", "must", "decoded to pixels no interpretation has"),
	("crash", "fatal", "crashed"),
	("timeout", "fatal", "timed out"),
	("oom", "fatal", "ran out of memory"),
	("memory", "fatal", "memory error under valgrind memcheck, whatever the outcome"),
	("api", "api", "returned without error, but not an image: wrong size, missing rows or non-numeric samples"),
	("shim", "shim", "the wrapper itself failed; listed under problems"),
]
THUMB_PX = 64  # the square box an output's thumbnail fits in; square, so a picture and its transpose get the same scale
# One line above each matrix-shaped table on what a click does there; the script shows them, since it does the work.
HINT = {t: f"<p class=js hidden>{text}</p>" for t, text in {
	"viol": "You can click any column header to make the table show only the rows where that implementation is above 0, and "
		"click it again to toggle this off. You can also click a row's name to sort the columns by it: fewest first, then most "
		"first, then back by name.",
	"feat": "You can click any column header to make the table show only the families that implementation fails in, "
		"or a family's name to make the results table show only that family.",
	"mx": "You can click any column header to make the table show only the tests that implementation fails. "
		"Click the same one again to toggle this filter off.",
}.items()}
EDITION = {"2": "2nd edition", "3": "3rd edition"}  # the lens names a reader sees
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
.m thead th .v{writing-mode:vertical-rl;transform:rotate(180deg);white-space:nowrap;display:inline-block}
.m thead tr.kinds th{text-align:center;height:1.4rem;line-height:1.4rem;padding:0 .2rem;color:#999;font-size:.8rem}
.m thead tr:not(.kinds) th{top:1.4rem}
th.ed{vertical-align:middle;text-align:center;padding:.2rem;color:#999;font-size:.8rem}
th.ed span{writing-mode:vertical-rl;transform:rotate(180deg);display:inline-block;white-space:nowrap}
#viol tbody,#feat tbody{display:none}
.eds{margin:.3rem 0}
#sum tr.off td:not(:first-child){opacity:.4}
th.asc::after{content:" ▴";color:#999}
th.desc::after{content:" ▾";color:#999}
.m th.pick{background:#24364f;color:#fff;box-shadow:inset 0 0 0 2px #8bbcff}
.m td.pick{box-shadow:inset 0 0 0 9rem rgba(139,188,255,.14)}
#viol td,#feat td{padding:0 .25rem}
.m .kb{border-left:3px solid #999}
.s tr.kb>td{border-top:3px solid #999}
.f tbody th,.v tbody th{white-space:nowrap}
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
.outs{display:grid;grid-template-columns:repeat(2,1fr);gap:.8rem 1rem;font-size:.85rem}
.outs div{display:flex;flex-direction:column;gap:.2rem}
.t{display:block;image-rendering:pixelated;background:var(--i) 0 0/100% 100%,repeating-conic-gradient(#777 0 25%,#555 0 50%) 0 0/8px 8px}
.legend td:first-child{text-align:center}
.note{color:#ddd}
code{font-size:.9em}
#mx,#mx thead,#mx tbody{display:block}
#mx thead{position:sticky;top:0;background:#151515;z-index:1}
#mx tr{display:grid;grid-template-columns:32rem repeat(var(--v),1.6rem)}
#mx tbody tr{content-visibility:auto;contain-intrinsic-size:auto 1.8rem}
#mx tr[hidden]{display:none}
#mx td,#mx th{display:block;box-sizing:border-box;border-width:0 1px 1px 0;grid-row:1}
#mx{border-left:1px solid #444;border-top:1px solid #444}
#mx thead th{position:static;padding:.3rem .1rem}
#mx tr.kinds th{height:auto;line-height:1.2}
#mx td:first-child,#mx th:first-child{grid-column:1}
#mx details[open]{width:auto}
#mx td>i{display:none;font-style:normal}
#mx td:has(>i){padding:0}
#mx th.ctl{display:grid;grid-template-columns:max-content 1fr;align-content:end;align-items:center;justify-items:start;gap:.3rem .5rem;padding:.3rem .4rem;text-align:left;font-size:.9rem}
#mx th.ctl label{display:contents}
#mx th.ctl label[hidden]{display:none}
#q{width:20rem}
#mx th.ctl label.gap>*{margin-top:.5rem}
#mx{position:relative;width:max-content}
.hl{position:absolute;display:none;pointer-events:none;background:rgba(255,255,255,.09);z-index:2}
#viol,#feat{position:relative}
#mx tr.kinds th.ctl{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.3rem 1rem}
""" + "".join(f".{cls}{{{style}}}" for cls, style in SEVERITY_STYLE.items()) + f".outs .b{{display:flex;align-items:end;height:{THUMB_PX}px}}"


def esc(s) -> str:
	return html.escape(str(s), quote=True)


# The kinds of column, in the order the tables show them: a validator's verdict on the file is context for reading
# the decoders' results, so it comes first after the reference.
KINDS = ("reference", "validators", "decoders")


def kind(impl: dict) -> str:
	return "reference" if impl["project"] == "reference" else "decoders" if impl["pixels"] else "validators"


def column_groups(report: dict) -> list[list[dict]]:
	"""Variants of one project whose results agree on every test share a column. Columns come by kind (KINDS) and
	alphabetically within it, where a reader expects a library to be."""
	def key(impl):
		rs = report["results"][impl["name"]]
		return impl["project"], json.dumps({t: {k: v for k, v in r.items() if k != "detail"} for t, r in rs.items()}, sort_keys=True)
	groups: dict[tuple, list[dict]] = {}
	for impl in report["implementations"]:
		groups.setdefault(key(impl), []).append(impl)
	return sorted(groups.values(), key=lambda g: (KINDS.index(kind(g[0])), g[0]["name"]))


def boundaries(groups: list[list[dict]]) -> set[int]:
	"""The index of the first column group of each kind but the first: the tables draw a heavier border there."""
	first: dict[str, int] = {}
	for n, g in enumerate(groups):
		first.setdefault(kind(g[0]), n)
	return {n for n in first.values() if n > 0}


def head_rows(groups: list[list[dict]], corner: str, matrix: bool = False, corner_span: int = 1, corner_top: str = "") -> str:
	"""The two header rows of a matrix-shaped table: the kinds spanning their columns, then the names, rotated. In the
	matrix (rows that are grids) a name cell carries its group's class c<N> and a kind cell its grid span; in a table
	the kinds span by colspan and the first column of a kind carries `kb`."""
	counts = {k: sum(kind(g[0]) == k for g in groups) for k in KINDS}
	bounds = boundaries(groups)
	kinds, at = [], 2
	for n, k in enumerate(KINDS):
		if counts[k]:
			span = f" style='grid-column:{at}/span {counts[k]}'" if matrix else f" colspan={counts[k]}"
			kinds.append(f"<th{span} data-k={n}{' class=kb' if n else ''}>{f'<span class=v>{k}</span>' if matrix else k}</th>")
			at += counts[k]
	names = []
	for n, g in enumerate(groups):
		cls = " ".join(c for c in (f"c{n}" if matrix else "", "kb" if n in bounds else "") if c)
		names.append(f"<th{f' class={chr(39)}{cls}{chr(39)}' if cls else ''} title='{esc(chr(10).join(label(i) for i in g))}'><span class=v>{esc(' · '.join(i['name'] for i in g))}</span></th>")
	corner_td = f"<th{f' colspan={corner_span}' if corner_span > 1 else ''}{' class=ctl' if matrix else ''}>"
	return f"<tr class=kinds>{corner_td}{corner_top}</th>{''.join(kinds)}</tr><tr>{corner_td}{corner if matrix else f'<span class=v>{corner}</span>'}</th>{''.join(names)}</tr>"


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
	text = next(iter(parts.values())) if len(set(parts.values())) == 1 else "; ".join(f"{EDITION[lens]}: {p}" for lens, p in parts.items())
	return text + (f"; strict: {', '.join(test['strict'])}" if test["strict"] else "")


def outcome(r: dict, lens: str) -> str:
	return r["outcome"] if isinstance(r["outcome"], str) else r["outcome"][lens]


def edition_boxes(table: str, lenses: list[str]) -> str:
	"""A checkbox per edition for one table (id <table>-e<lens>); the third edition starts ticked. Pure CSS: the
	rules in edition_css() show what is ticked, or everything when nothing is."""
	assert "3" in lenses, "the third edition is the one ticked at first"
	return "<p class=eds>" + " ".join(f"<label><input type=checkbox id={table}-e{lens}{' checked' if lens == '3' else ''}> {EDITION[lens]}</label>"
		for lens in lenses) + "</p>"


def edition_css(lenses: list[str]) -> str:
	"""The editions each table shows. Tables with a block of rows per edition (tbody.e<lens>) show the ticked
	blocks, with a bar between two shown blocks; the matrix shows, in a cell where the editions disagree, the ticked
	editions' glyphs (td>i.e<lens>) stacked with a bar between; the show select's `any` option hides a row that passes
	under every ticked edition. Nothing ticked shows no rows."""
	out = []
	for t in ("viol", "feat"):
		out += [f"body:has(#{t}-e{l}:checked) #{t} tbody.e{l}{{display:table-row-group}}" for l in lenses]
		out += [f"body:has(#{t}-e{a}:checked):has(#{t}-e{b}:checked) #{t} tbody.e{b} tr:first-child>*{{border-top:3px solid #999}}"
			for a, b in zip(lenses, lenses[1:])]
	out += [f"body:has(#mx-e{l}:checked) #mx td>.e{l}{{display:block}}" for l in lenses]
	out += [f"body:has(#mx-e{a}:checked):has(#mx-e{b}:checked) #mx td>.e{b}{{border-top:2px solid #999}}" for a, b in zip(lenses, lenses[1:])]
	none = "".join(f":not(:has(#mx-e{l}:checked))" for l in lenses)
	out.append(f"body{none} #mx tbody{{display:none}}")
	for picked in range(1, 1 << len(lenses)):  # every non-empty choice of editions
		chosen = [l for n, l in enumerate(lenses) if picked >> n & 1]
		cond = "".join(f":has(#mx-e{l}:checked)" if l in chosen else f":not(:has(#mx-e{l}:checked))" for l in lenses)
		out.append(f"body:has(#f option[value=any]:checked){cond} #mx tr{''.join(f'.ok{l}' for l in chosen)}{{display:none}}")
	return "".join(out)


def split_td(per_lens: dict[str, tuple[str, str]], extra: str = "", cls: str = "") -> str:
	"""A matrix cell: when the editions agree, the text with its class; otherwise one <i> per edition, each with its
	own class, which edition_css() shows or hides. `extra` is edition-independent HTML after the text; `cls` the
	cell's own classes (its column class c<N>)."""
	if len(set(per_lens.values())) == 1:
		sev, text = next(iter(per_lens.values()))
		classes = " ".join(c for c in (cls, sev) if c)
		return f"<td{f' class={chr(39)}{classes}{chr(39)}' if classes else ''}>{esc(text)}{extra}</td>"
	inner = "".join(f"<i class='e{lens}{f' {sev}' if sev else ''}'>{esc(text)}</i>" for lens, (sev, text) in per_lens.items())
	return f"<td{f' class={chr(39)}{cls}{chr(39)}' if cls else ''}>{inner}{extra}</td>"


def cell(r: dict, lenses: list[str], ratings: dict[str, str], col: int) -> str:
	"""One matrix cell in column group `col`: under each edition, the outcome's glyph colored by its severity (no
	class for a pass, the column's default); a crash, hang or out-of-memory is `fatal`, and a memory error shows as
	M whatever the pixels; `api` and `shim` are their own classes. The arguable marks are edition-independent."""
	mark = marks(r, ratings) if "memory" not in r else ""
	sup = f"<sup>{mark}</sup>" if mark else ""
	if "memory" in r:
		return f"<td class='c{col} fatal'>{GLYPH['memory']}{sup}</td>"
	per = {}
	for lens in lenses:
		o = outcome(r, lens)
		sev = "fatal" if o in ("crash", "timeout", "oom") else "api" if o == "api" else r["severity"][lens]
		per[lens] = ("" if sev == "pass" else sev, GLYPH[o])
	return split_td(per, sup, f"c{col}")


def matrix_vars(groups: list[list[dict]], hidden: set[int] = frozenset()) -> str:
	"""The matrix's column placement as CSS, which the script rewrites to re-sort or hide columns: --v (visible
	columns), --c<N> (each group's track), display:none for hidden groups, and the kind boundaries' border."""
	shown = [n for n in range(len(groups)) if n not in hidden]
	css = [f"#mx{{--v:{len(shown)};" + "".join(f"--c{n}:{shown.index(n) + 2};" for n in shown) + "}"]
	css += [f"#mx .c{n}{{display:none}}" for n in sorted(hidden)]
	first: dict[str, int] = {}
	for n in shown:
		first.setdefault(kind(groups[n][0]), n)
	css += [f"#mx .c{n}{{border-left:3px solid #999}}" for k, n in first.items() if KINDS.index(k)]
	return "".join(css)


def violation(test: dict, impl: dict, r: dict, lens: str) -> str | None:
	"""The SUMMARY cell one result counts in under a lens, or None when it passes. For a decoder, with E the
	lens's expectation for the file:
	- `rejected` (valid file rejected): outcome reject with E display.
	- `accepted` (invalid file accepted): outcome accept with E must-reject.
	- `wrong` (wrong pixels): outcome wrong, E display, recover or may-reject.
	- `fatal`: outcome crash, timeout or oom, any E; or a memory error under valgrind, any outcome.
	- `api`: outcome api (no usable image returned), any E.
	- `should` (recoverable file rejected): outcome reject with E recover.
	A validator is judged on whether the file conforms: `rejected` is a reject of a conforming file that
	carries no strict note, `accepted` an accept of a file that does not conform (outcome accept), and
	`fatal` and `api` as for a decoder. A wrapper failure (outcome shim) is not a violation. Every
	must-violation is in exactly one of the five kinds."""
	o, sev, e = outcome(r, lens), r["severity"][lens], test["expectation"][lens]
	if "memory" in r or o in ("crash", "timeout", "oom"):
		assert sev == "must", (test["id"], impl["name"], r)
		return "fatal"
	if o == "api":
		assert sev == "must", (test["id"], impl["name"], r)
		return "api"
	if o == "shim":
		assert sev == "shim", (test["id"], impl["name"], r)
		return None
	if sev == "pass":
		return None
	if impl["pixels"]:
		kind = {("reject", "display"): "rejected", ("reject", "recover"): "should", ("accept", "must-reject"): "accepted"}.get((o, e))
		kind = kind or ("wrong" if o == "wrong" and e != "must-reject" else None)
	else:
		kind = {("reject", True): "rejected", ("accept", False): "accepted"}.get((o, test["conforms"][lens]))
	assert kind and (sev == "should") == (kind == "should"), (test["id"], impl["name"], lens, o, sev, e)
	return kind


SUMMARY = {"rejected": "valid file rejected", "accepted": "invalid file accepted", "wrong": "wrong pixels",
	"fatal": "crash, hang, out of memory or memory error", "api": "no usable image returned", "should": "recoverable file rejected"}


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
	"""An image in its box (its CSS class holds the PNG and its size), or its size when report.json has no PNG of it."""
	im = images[digest]
	inner = f"<i class='t {classes[digest]}'></i>" if "png" in im else f"{im['width']}×{im['height']}"
	return f"<span class=b>{inner}</span>"


def thumb_size(im: dict) -> tuple[int, int]:
	"""A thumbnail's size: the image at the largest whole-number scale that fits the box, so its pixels are even,
	or scaled down to fit when it is larger than the box."""
	k = min(THUMB_PX / im["width"], THUMB_PX / im["height"])
	k = int(k) if k >= 1 else k
	return max(1, round(im["width"] * k)), max(1, round(im["height"] * k))


def thumb_css(images: dict, classes: dict[str, str]) -> str:
	out = []
	for digest, cls in classes.items():
		im = images[digest]
		w, h = thumb_size(im)
		out.append(f".{cls}{{width:{w}px;height:{h}px;--i:url(data:image/png;base64,{im['png']})}}")
	return "".join(out)


def bucket_label(b: dict, r: dict, ratings: dict[str, str]) -> str:
	"""Which interpretations a bucket matches, with the topics of an arguable rating and the match's properties. A
	bucket that matches none is labelled with the generator's explanation of those pixels when it has one, else
	with how they differ from the expected ones; a difference needs no further comment to read as wrong."""
	tag = b["label"]
	if "explanation" in b:
		return b["explanation"]
	if tag == "unexplained":
		return r["mismatch"]
	if tag == "unverifiable":
		return "no reference pixels"
	# `none` is the interpretation of a file with no open decision
	text = "matches " + (" or ".join(c for c in tag["choices"] if c != "none") if tag["choices"] != ["none"] else "the reference")
	mark = marks(r, ratings)
	if mark:
		text += " (arguable: " + "; ".join(TOPIC[t][1] for t, (m, _) in TOPIC.items() if m in mark) + ")"
	if "alpha0" in tag:
		text += f"; color under alpha 0 {tag['alpha0']}"
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
		# The expected picture once, with how many columns produced exactly it. Its columns go unnamed only while
		# it outnumbers every other bucket, so that the names listed are the exceptions.
		exact = [b for b in test["buckets"] if b["digest"] == test["preferred"]]
		others = max((b["count"] for b in test["buckets"] if b["digest"] != test["preferred"]), default=0)
		matched = f"{exact[0]['count']} match" + ("" if exact[0]["count"] > others else ": " + esc(", ".join(exact[0]["columns"]))) if exact else "none match"
		outs.append(f"<div>{thumb(test['preferred'], report['images'], classes)}<span>expected</span><span>{matched}</span></div>")
	for b in test["buckets"]:
		if b["digest"] == test["preferred"]:
			continue
		r = report["results"][b["columns"][0]][test["id"]]
		outs.append(f"<div>{thumb(b['digest'], report['images'], classes)}<span>{esc(bucket_label(b, r, report['ratings']))}</span>"
			f"<span>{b['count']}: {esc(', '.join(b['columns']))}</span></div>")
	if outs:
		out.append("<div class=outs>" + "".join(outs) + "</div>")
	return "<div class=x>" + "".join(out) + "</div>"


def legend_class(r: dict, lens: str) -> tuple[str, str]:
	"""(outcome, severity class) of one result under one lens, as cell() draws it."""
	o = "memory" if "memory" in r else outcome(r, lens)
	return o, "fatal" if o in ("crash", "timeout", "oom", "memory") else "api" if o == "api" else r["severity"][lens]


def legend(report: dict) -> str:
	"""Every kind of cell in the matrix, drawn as the matrix draws it, with what it counts as and what happened.
	A result outside LEGEND is an error: the legend would be incomplete."""
	seen = {legend_class(r, lens) for cells in report["results"].values() for r in cells.values() for lens in report["lenses"]}
	assert seen <= {(o, sev) for o, sev, _ in LEGEND}, seen - {(o, sev) for o, sev, _ in LEGEND}
	rows = []
	for o, sev, text in LEGEND:
		rows.append(f"<tr><td class={sev}>{GLYPH[o]}</td><td class={sev}>{COUNTS_AS[sev]}</td><td>{esc(text)}</td></tr>")
		if o == "pixels":  # the arguable marks, on a check mark or an A
			rows += [f"<tr><td class=pass>✓<sup>{m}</sup></td><td class=pass>pass</td><td>{esc(t)}: an arguable choice; the letter also marks an A</td></tr>"
				for m, t in TOPIC.values()]
	return "<h2>Legend</h2><table class=legend><tr><th>cell</th><th>counts as</th><th>meaning</th></tr>" + "".join(rows) + "</table>"


def families(report: dict) -> list[str]:
	return list(dict.fromkeys(t["family"] for t in report["tests"]))


def feature_table(report: dict, groups: list[list[dict]]) -> str:
	"""Per family and column group, under each edition: n/m, the n of the family's m judged tests the group fails
	(must or should), colored by the worst severity; a pass glyph when n is 0; blank when a wrapper failure left
	nothing judged. A block of rows per edition, labelled down its side; edition_css() shows the ticked ones."""
	lenses = report["lenses"]
	bounds = boundaries(groups)
	fams = families(report)
	out = [f"<h2>By family</h2>{HINT['feat']}{edition_boxes('feat', lenses)}<table class='m f' id=feat><thead>{head_rows(groups, 'family', corner_span=2)}</thead>"]
	for lens in lenses:
		out.append(f"<tbody class=e{lens}>")
		for row, fam in enumerate(fams):
			tests = [t for t in report["tests"] if t["family"] == fam]
			cells = []
			for col, g in enumerate(groups):
				rs = report["results"][g[0]["name"]]
				sev = [rs[t["id"]]["severity"][lens] for t in tests if rs[t["id"]]["severity"][lens] != "shim"]
				n = sum(x != "pass" for x in sev)
				cls = ("must" if "must" in sev else "should") if n else "pass"
				cells.append(f"<td class='{cls}{' kb' if col in bounds else ''}'>{f'{n}/{len(sev)}' if n else '✓' if sev else ''}</td>")
			side = f"<th class=ed rowspan={len(fams)}><span>{EDITION[lens]}</span></th>" if row == 0 else ""
			out.append(f"<tr>{side}<th><span class=fam>{esc(fam)}</span></th>{''.join(cells)}</tr>")
		out.append("</tbody>")
	return "".join(out) + "</table>"


def alpha0_text(by_source: dict[str, dict[str, int]]) -> tuple[str, str]:
	"""The color under fully transparent pixels, and the cell's class: one word when the column always does the same;
	per source of the transparency when each source has one behavior; else each behavior with its count."""
	values = {v for d in by_source.values() for v in d}
	cls = " class=should" if "altered" in values else ""
	if len(values) <= 1:
		return ", ".join(values), cls
	if all(len(d) == 1 for d in by_source.values()):
		return "; ".join(f"{src}: {next(iter(d))}" for src, d in by_source.items()), cls
	counts: dict[str, int] = {}
	for d in by_source.values():
		for v, n in d.items():
			counts[v] = counts.get(v, 0) + n
	return ", ".join(f"{v} {n}" for v, n in sorted(counts.items(), key=lambda x: -x[1])), cls


def implementations_table(report: dict, groups: list[list[dict]]) -> str:
	"""Each column group's names, versions and lineage, behind a checkbox (script only) that shows or hides its
	column everywhere; the rows carry what the script sorts the columns by. The script sorts the table itself by the
	header a reader clicks."""
	lenses = report["lenses"]
	order = {i["name"]: n for n, i in enumerate(report["implementations"])}
	first = {kind(g[0]): g for g in reversed(groups)}  # the first group of each kind
	out = ["<h2>Implementations</h2><table class=s id=sum><thead><tr><th class=js hidden>show</th><th>implementation</th><th>version</th><th>lineage</th><th>diagnostics</th></tr></thead><tbody>"]
	for n, g in enumerate(groups):
		s = g[0]["summary"]
		names = "<br>".join(esc(i["name"]) for i in g)
		vers = "<br>".join(esc(i["revision"] or i["version"] or "") for i in g)
		# report.py also renders saved JSON from before diagnostic metadata was recorded.
		diagnostics = "<br>".join(esc(v) for v in dict.fromkeys(i.get("diagnostics", "not recorded") for i in g))
		sort = "".join(f" data-m{lens}={s[lens]['must']} data-s{lens}={s[lens]['should']}" for lens in lenses)
		k = KINDS.index(kind(g[0]))
		kb = " class=kb" if first[kind(g[0])] is g and k else ""
		out.append(f"<tr{kb}{sort} data-o={order[g[0]['name']]} data-k={k} data-n='{esc(g[0]['name'])}'><td class=js hidden><input type=checkbox checked data-g={n}></td>"
			f"<td title='{esc(g[0]['notes'])}'>{names}</td><td>{vers}</td><td>{esc(g[0]['lineage'])}</td><td>{diagnostics}</td></tr>")
	return "".join(out) + "</tbody></table>"


def violations_table(report: dict, groups: list[list[dict]]) -> str:
	"""How many tests each column group counts in each SUMMARY kind, then its silent and strict counts: a row per
	kind and a column per group, as the matrix has them, in a block of rows per edition labelled down its side."""
	lenses = report["lenses"]
	bounds = boundaries(groups)
	out = [f"<h2>Violations</h2>{HINT['viol']}{edition_boxes('viol', lenses)}<table class='m v' id=viol><thead>{head_rows(groups, '', corner_span=2)}</thead>"]
	for lens in lenses:
		rows: list[tuple[str, list[str]]] = [(SUMMARY[k], []) for k in SUMMARY] + [("silent", []), ("strict", [])]
		for n, g in enumerate(groups):
			s, c = g[0]["summary"], counts(report, g[0])
			kb = " kb" if n in bounds else ""
			for row, k in enumerate(SUMMARY):
				cls = (k if k == "should" else "must") if c[lens][k] else "pass"
				rows[row][1].append(f"<td class='{cls}{kb}'>{c[lens][k]}</td>")
			rows[-2][1].append(f"<td{f' class=kb' if kb else ''}>{s[lens]['silent']}</td>")
			rows[-1][1].append(f"<td{f' class=kb' if kb else ''}>{s.get('strict', '')}</td>")
		out.append(f"<tbody class=e{lens}>")
		for row, (name, cells) in enumerate(rows):
			side = f"<th class=ed rowspan={len(rows)}><span>{EDITION[lens]}</span></th>" if row == 0 else ""
			out.append(f"<tr>{side}<th>{esc(name)}</th>{''.join(cells)}</tr>")
		out.append("</tbody>")
	out.append("</table><p>The first five rows are must-violations and the sixth should-violations. "
		"<b>silent</b>: the column accepted a file that does not conform and said nothing about it; a count, not a violation. "
		"<b>strict</b>: a validator rejected a file that conforms, over a name a strict reading of the naming rules may flag "
		"(iDOT's unregistered public name); recorded, not a violation.</p>")
	return "".join(out)


def names_text(groups: list[list[dict]]) -> str:
	"""Column groups in prose, by project, each once: `a, b and c`."""
	names = list(dict.fromkeys(i["project"] for g in groups for i in g))
	return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


VERB = {"preserved": "preserve", "zeroed": "zero", "altered": "change"}
SOURCE_TEXT = {"alpha channel": "an alpha channel", "tRNS color key": "a tRNS color key", "tRNS palette": "a tRNS palette"}


def deviation(by_source: dict[str, dict[str, int]], norm: str) -> str:
	"""How a column's color under alpha 0 departs from the norm, in words: per behavior, the sources it shows under;
	a source with mixed behavior is given with its counts."""
	parts, mixed = {}, []
	for src, d in by_source.items():
		if len(d) == 1:
			v = next(iter(d))
			if v != norm:
				parts.setdefault(v, []).append(src)
		else:
			mixed.append(f"under {SOURCE_TEXT[src]} " + " and ".join(f"{v} on {n}" for v, n in d.items()))
	return "; ".join([f"they {VERB[v]} it under " + " and ".join(SOURCE_TEXT[s] for s in srcs) for v, srcs in parts.items()] + mixed)


def properties(report: dict, groups: list[list[dict]]) -> str:
	"""Two short paragraphs: how the decoders that return at most 8 bits reduce 16-bit samples, and the color under
	fully transparent pixels, as the norm and the exceptions by name, since nearly every decoder does the same."""
	out = []
	eight = [g for g in groups if g[0]["pixels"] and g[0]["max_depth"] == 8]
	if eight:
		ways: dict[str, list] = {}
		for g in eight:
			ways.setdefault(", ".join(g[0]["summary"].get("16to8", [])) or "showed no 16-bit image", []).append(g)
		text = f"{names_text(eight)} return at most 8 bits per sample. "
		text += "All of them keep the high byte of a 16-bit sample and drop the low byte." if list(ways) == ["high byte"] \
			else " ".join(f"{names_text(gs)}: {way}." for way, gs in ways.items())
		out.append(f"<p>{esc(text)}</p>")
	by_text: dict[str, list] = {}
	for g in groups:
		if g[0]["pixels"]:
			by_text.setdefault(alpha0_text(g[0]["summary"].get("alpha0", {}))[0], []).append(g)
	norm = max(by_text, key=lambda t: len(by_text[t]))
	if norm in VERB:
		text = f"Every decoder {VERB[norm]}s the color under fully transparent pixels"
		exceptions = []
		for t, gs in by_text.items():
			if t != norm:
				dev = deviation(gs[0][0]["summary"]["alpha0"], norm)
				clause = esc(f"{names_text(gs)}: {dev}")
				exceptions.append(f"<span class=should>{clause}</span>" if "change" in dev else clause)
		out.append(f"<p>{esc(text)}" + (", except " + "; ".join(exceptions) if exceptions else "") + ".</p>")
	else:
		out.append("<p>The color under fully transparent pixels: " + "; ".join(f"{esc(names_text(gs))}: {esc(t)}" for t, gs in by_text.items()) + ".</p>")
	return "".join(out)


def disclaimer() -> str:
	"""The README's "Human disclaimer" section, verbatim, rendered as CommonMark; the README is its only copy."""
	head = "\n## Human disclaimer\n\n"
	text = README.read_text(encoding="utf-8")
	assert text.count(head) == 1, f"{README} must have exactly one '## Human disclaimer' section"
	body = text.split(head, 1)[1].split("\n## ", 1)[0].strip()
	assert body, f"{README}: the '## Human disclaimer' section is empty"
	return "<h2>Human disclaimer</h2>" + MarkdownIt("commonmark").render(body)


def filter_form(report: dict) -> str:
	"""The row filters and the column sort, one labelled line each. `show` works without the script: edition_css()
	hides the passing rows when its `any` option is chosen. The script adds its per-column options (the tests one
	column fails, also reached by clicking the column's header in any of the three matrix-shaped tables) and shows
	the other controls."""
	return ("<label><span>show</span><select id=f><option value=''>every test<option value=any>tests some shown implementation fails</select></label>"
		"<label class=js hidden><span>family</span><select id=fam><option value=''>every family" + "".join(f"<option>{esc(f)}" for f in families(report)) + "</select></label>"
		"<label class=js hidden><span>expectation</span><select id=ex><option value=''>any" + "".join(f"<option>{e}" for e in report["expectations"][::-1]) + "</select></label>"
		"<label class=js hidden><span>search</span><input id=q type=search placeholder='id or note'></label>"
		"<label class='js gap' hidden><span>sort columns</span><select id=sort><option value=n>by name<option value=v>by violations</select></label>")


def render(report: dict) -> str:
	groups = column_groups(report)
	tests = report["tests"]
	lenses = report["lenses"]
	date = report["generated"][:10]
	assert {c.split(":")[0] for c in report["ratings"]} == set(TOPIC), "a choice topic TOPIC has no letter for"
	assert set(lenses) <= set(EDITION), lenses
	digests = sorted({d for t in tests for d in [*(b["digest"] for b in t["buckets"]), t["preferred"]] if d and "png" in report["images"][d]})
	classes = {d: f"i{n}" for n, d in enumerate(digests)}
	columns = "".join(f"#mx .c{n}{{grid-column:var(--c{n})}}" for n in range(len(groups)))
	out = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
		f"<title>PNG decoder conformance, {date}</title><style>{CSS}{edition_css(lenses)}{columns}{thumb_css(report['images'], classes)}</style>"
		f"<style id=dyn>{matrix_vars(groups)}</style>"]
	out.append(f"""<h1>PNG decoder conformance</h1>
<p>Every decoder below was run over the same {len(tests)} small PNG files from <a href="{REPO}">difftest-png</a>, each one built to test one rule of the PNG specification or one behavior on which some decoder deviates. Run {esc(report['generated'])} on {esc(report['host'])}.</p>
<p>Each result is rated under each lens: <b>pass</b>, <b class=should>should</b> (the specification says a decoder should do otherwise, e.g. recover a recoverable fault instead of rejecting the image) or <b class=must>must</b> (a requirement is broken: wrong pixels, an invalid file accepted, a valid file rejected, a result that is not an image, a crash, a hang, a memory error). Warnings never count against a decoder. Running out of memory counts like a crash: a file should be rejected with an error, and a decoder that instead exhausts its host hands every program that opens untrusted images a denial of service. Pixels are compared as 16-bit RGBA with no color management; color under fully transparent pixels and the 8-bit reduction of 16-bit images are recorded as properties, not failures. Validators (pngcheck) are judged on a different question: whether each file conforms to the specification. Some files a decoder must still display do not conform, for example one with a chunk name the specification reserves.</p>
<p>Two editions of the specification matter: the second (ISO/IEC 15948:2003; PNG 1.0 and 1.1 count as the second) and the third (W3C, 2025). A lens is an edition's text plus the extensions: the W3C registered extensions, Apple's iDOT, and under the second edition the chunks the third edition added (APNG, cICP, mDCV, cLLI, eXIf). Every result is rated through both lenses separately; each table has a checkbox per edition, and shows the ticked ones. There is no single verdict: following one edition rather than the other is not a defect. The two ratings differ only where the texts contradict each other, which they do in one place: a palette index past the end of PLTE is an error in the second edition and renders opaque black in the third.</p>
<p>Not tested: animation frames after the default image, color management (gAMA, cHRM, iCCP, sRGB, cICP, sBIT), encoders, performance. Passing every test means the suite is incomplete, not that the decoder is perfect.</p>""")
	if report["problems"]:
		out.append("<p class=must>Suite consistency problems in this run:</p><ul>" + "".join(f"<li>{esc(p)}" for p in report["problems"]) + "</ul>")

	out.append(disclaimer())
	out.append(legend(report))
	out.append(implementations_table(report, groups))
	out.append(violations_table(report, groups))
	out.append(properties(report, groups))
	out.append(feature_table(report, groups))
	# The matrix's controls sit in its sticky header: the editions and the count of shown rows in the top corner,
	# the row filters and the column sort as a labelled form in the tall corner under it.
	top = edition_boxes("mx", lenses).replace("<p class=eds>", "<span>").replace("</p>", "</span>") + "<span id=n class=js hidden></span>"
	out.append(f"<h2 id=results>Results</h2>{HINT['mx']}<table class=m id=mx><thead>{head_rows(groups, filter_form(report), matrix=True, corner_top=top)}</thead><tbody>")
	for t in tests:
		rs = [report["results"][g[0]["name"]][t["id"]] for g in groups]
		ok = "".join(f" ok{lens}" for lens in lenses if all(r["severity"][lens] == "pass" for r in rs)).strip()
		data = "".join(f" data-e{lens}={t['expectation'][lens]}" for lens in lenses)
		out.append(f"<tr{f' class={chr(39)}{ok}{chr(39)}' if ok else ''}{data} data-f='{esc(t['family'])}'><td><details><summary><span>{esc(t['id'])}</span>"
			f"<a href='{REPO}/blob/HEAD/decode/{esc(t['file'])}'>png</a></summary>{expansion(t, report, classes)}</details></td>"
			f"{''.join(cell(r, lenses, report['ratings'], n) for n, r in enumerate(rs))}</tr>")
	out.append("</tbody></table>")
	out.append(f"<script>{SCRIPT}</script>")
	return "".join(out)


SCRIPT = """
const $ = (s, e = document) => [...e.querySelectorAll(s)];
const byId = id => document.getElementById(id);
for (const e of $(".js")) e.hidden = false;
const sumRows = $("#sum tbody tr");
// The two small tables move their cells; the matrix is placed by the CSS in #dyn (grid tracks per column class).
// A row's column cells: its tds, and in the header the name cells, which alone carry a title.
const cellsOf = new Map([...$("#viol tr:not(.kinds)"), ...$("#feat tr:not(.kinds)")].map(r => [r, [...r.children].filter(c => c.tagName == "TD" || c.hasAttribute("title"))]));
const groups = sumRows.map((tr, i) => ({i, tr, name: tr.dataset.n, o: +tr.dataset.o, k: +tr.dataset.k, on: true}));
const cmp = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const dyn = byId("dyn"), sortBy = byId("sort"), viol = byId("viol"), feat = byId("feat");
const ticked = t => $(`[id^=${t}-e]`).filter(e => e.checked).map(e => e.id.slice(-1));
// Each table orders its own columns, by kind first. The matrix's are by name or, as its sort select says, by
// violations under its ticked edition (the third when both are); the violations table's by the row a reader
// clicked, fewest first, on a second click most first, on a third by name again; the family table's by name.
// The implementations table sorts its rows by its headers (sortSum).
let mxOrder = groups, violOrder = groups, sortRow = null, sortDir = 0;
const edition = () => { const e = ticked("mx"); return e.includes("3") || !e.length ? "3" : e[0]; };
function sort() {
	const L = edition(), d = g => g.tr.dataset;
	mxOrder = sortBy.value == "n" ? groups : [...groups].sort((a, b) => a.k - b.k || d(a)["m" + L] - d(b)["m" + L] || d(a)["s" + L] - d(b)["s" + L] || cmp(a.name, b.name));
	const cells = sortRow && cellsOf.get(sortRow), v = g => +cells[g.i].textContent || 0;
	violOrder = sortRow ? [...groups].sort((a, b) => a.k - b.k || sortDir * (v(a) - v(b)) || cmp(a.name, b.name)) : groups;
	place();
}
for (const tr of $("#viol tbody tr")) {
	const th = tr.querySelector("th:not(.ed)");
	th.style.cursor = "pointer";
	th.onclick = () => {
		sortDir = tr === sortRow ? (sortDir == 1 ? -1 : 0) : 1;
		sortRow = sortDir ? tr : null;
		for (const t of $("#viol tbody th")) { t.classList.toggle("asc", t === th && sortDir == 1); t.classList.toggle("desc", t === th && sortDir == -1); }
		sort();
	};
}
// A table's columns in `order`: its cells moved, and its kind bars on the first shown column of each kind, since
// it drops unticked columns.
const rowsOf = t => [...cellsOf].filter(([r]) => t.contains(r));
function arrange(t, order) {
	const seen = new Set();
	for (const [r, cells] of rowsOf(t)) r.append(...order.map(g => cells[g.i]));
	for (const g of order.filter(g => g.on)) {
		const first = !seen.has(g.k) && g.k > 0;
		seen.add(g.k);
		for (const [, cells] of rowsOf(t)) cells[g.i].classList.toggle("kb", first);
	}
}
// The matrix's placement CSS from its order, the two tables' cells from theirs, and every table's kind spans.
function place() {
	const shown = mxOrder.filter(g => g.on);
	const seen = new Set(), n = {}, css = [`#mx{--v:${shown.length};` + shown.map((g, i) => `--c${g.i}:${i + 2};`).join("") + "}"];
	let at = 2;
	for (const g of shown) {
		const first = !seen.has(g.k) && g.k > 0;
		seen.add(g.k);
		n[g.k] = (n[g.k] || 0) + 1;
		if (first) css.push(`#mx .c${g.i}{border-left:3px solid #999}`);
	}
	for (const g of groups) if (!g.on) css.push(`#mx .c${g.i}{display:none}`);
	for (const th of $("tr.kinds th[data-k]")) {
		const c = n[th.dataset.k] || 0;
		th.hidden = !c;
		if (th.closest("#mx")) { th.style.gridColumn = `${at}/span ${c || 1}`; at += c; } else th.colSpan = c || 1;
	}
	dyn.textContent = css.join("");
	arrange(viol, violOrder);
	arrange(feat, groups);
}
// The implementations table sorts by the header a reader clicks, within each kind, and in reverse on a second
// click. It keeps unticked rows, dimmed, so its bars follow the first row of each kind.
const heads = $("#sum thead th");
let sumBy = 1, sumDir = 1;
function sortSum() {
	const text = g => g.tr.children[sumBy].textContent.toLowerCase();
	const order = [...groups].sort((a, b) => a.k - b.k || sumDir * text(a).localeCompare(text(b), undefined, {numeric: true}) || cmp(a.name, b.name));
	const first = new Set();
	for (const g of order) { g.tr.parentNode.append(g.tr); g.tr.classList.toggle("kb", !first.has(g.k) && g.k > 0); first.add(g.k); }
	heads.forEach((th, i) => { th.classList.toggle("asc", i == sumBy && sumDir > 0); th.classList.toggle("desc", i == sumBy && sumDir < 0); });
}
heads.slice(1).forEach((th, i) => { th.style.cursor = "pointer"; th.onclick = () => { sumDir = i + 1 == sumBy ? -sumDir : 1; sumBy = i + 1; sortSum(); }; });
const picks = $("[data-g]");
for (const c of picks) c.onchange = () => {
	const g = groups[c.dataset.g];
	g.on = c.checked;
	g.tr.classList.toggle("off", !g.on);
	for (const cells of cellsOf.values()) cells[g.i].hidden = !g.on;
	place();
	filter();
};
const rows = $("#mx tbody tr");
const text = rows.map(r => [r.querySelector("summary span"), r.querySelector(".note")].map(e => e.textContent).join(" ").concat(" ", r.dataset.f).toLowerCase());
// Which columns fail each row under each edition, read once from the cells' classes, so the failing-only filter
// counts only the columns that are shown. The script then owns that filter: the rows' ok classes, which the CSS
// filter uses without a script, come off.
const SEV = ["must", "should", "fatal", "api"], EDS = $("[id^=mx-e]").map(e => e.id.slice(-1));
const fails = rows.map(r => {
	const f = Object.fromEntries(EDS.map(L => [L, new Set()]));
	for (const td of [...r.children].slice(1)) {
		const col = +[...td.classList].find(c => /^c\\d+$/.test(c)).slice(1);
		if (SEV.some(s => td.classList.contains(s))) { for (const L of EDS) f[L].add(col); continue; }
		for (const i of td.children) if (SEV.some(s => i.classList.contains(s))) f[EDS.find(L => i.classList.contains("e" + L))].add(col);
	}
	return f;
});
for (const r of rows) r.classList.remove(...EDS.map(L => "ok" + L));
// The show select's per-column options: the tests one column fails, in the header's order under each kind.
// Clicking a column's header in any of the three tables picks it too, and again unpicks it. The picked column's
// headers and cells show it (mark), and each table narrows its rows to it its own way (narrow).
const show = byId("f"), names = $("#mx thead tr:not(.kinds) th").slice(1), pk = document.head.appendChild(document.createElement("style"));
const kindName = Object.fromEntries($("#mx tr.kinds th[data-k]").map(th => [th.dataset.k, th.textContent]));
for (const [k, name] of Object.entries(kindName)) {
	const og = show.appendChild(Object.assign(document.createElement("optgroup"), {label: name}));
	for (const g of groups) if (g.k == k) og.append(new Option(`tests ${names[g.i].textContent} fails`, "c" + g.i));
}
const inHead = ([r]) => r.parentElement.tagName == "THEAD", headers = [...cellsOf].filter(inHead).map(([, cells]) => cells);
const picked = () => show.value.startsWith("c") ? +show.value.slice(1) : -1;
function mark() {
	const c = picked();
	groups.forEach((g, i) => { for (const cells of [names, ...headers]) cells[i].classList.toggle("pick", i == c); });
	for (const [r, cells] of cellsOf) if (!inHead([r])) cells.forEach((cell, i) => cell.classList.toggle("pick", i == c));
	pk.textContent = c < 0 ? "" : `#mx .c${c}{box-shadow:inset 0 0 0 3rem rgba(139,188,255,.14)}`;
}
function pick(i) {
	show.value = show.value == "c" + i ? "" : "c" + i;
	filter();
}
for (const cells of [names, ...headers]) cells.forEach((th, i) => { th.style.cursor = "pointer"; th.onclick = () => pick(i); });
// The two summary tables keep, for a picked column, the rows where it is above 0 (a family it fails in, a kind
// of result it has), the way the matrix keeps the tests it fails. The edition label down a block's side moves
// to the first row left, with the block's remaining height.
function narrow() {
	const c = picked();
	for (const t of [viol, feat]) for (const tb of t.tBodies) {
		const ed = tb.querySelector("th.ed"), rows = [...tb.rows];
		for (const r of rows) r.hidden = c >= 0 && !(+cellsOf.get(r)[c].textContent.split("/")[0]);
		const left = rows.filter(r => !r.hidden);
		if (left.length) { ed.rowSpan = left.length; left[0].prepend(ed); }
	}
}
const q = byId("q"), fam = byId("fam"), ex = byId("ex");
function filter() {
	const eds = ticked("mx"), s = q.value.toLowerCase(), want = show.value;
	const failing = want == "any" ? i => eds.some(L => [...fails[i][L]].some(c => groups[c].on))
		: want ? (c => i => eds.some(L => fails[i][L].has(c)))(+want.slice(1)) : () => true;
	rows.forEach((r, i) => r.hidden = !(text[i].includes(s) && (!ex.value || eds.some(L => r.dataset["e" + L] == ex.value))
		&& (!fam.value || r.dataset.f == fam.value) && failing(i)));
	count();
	mark();
	narrow();
}
// How many rows the filters leave, whichever of them (script or CSS) hid the rest.
const n = byId("n");
function count() { n.textContent = `${rows.filter(r => r.checkVisibility()).length} of ${rows.length} tests`; }
for (const s of $(".fam")) {
	const b = document.createElement("button");
	b.textContent = s.textContent;
	b.onclick = () => { fam.value = s.textContent; filter(); byId("results").scrollIntoView(); };
	s.replaceWith(b);
}
for (const e of $("[id^=mx-e]")) e.addEventListener("change", () => { filter(); if (sortBy.value == "v") sort(); });
sortBy.onchange = sort;
q.oninput = fam.onchange = ex.onchange = show.onchange = filter;
sortSum();
sort();
filter();
// Hover, in the matrix and the two matrix-shaped tables: a translucent strip over the row and one over the
// column, placed from the cell under the mouse. They overlap on that cell alone, so it is the brightest; above the
// sticky header, so the column's name lights up too. Over a row's label only the row lights up, over a column's
// name or a kind only the column(s); the corner and the edition labels down the side light nothing.
for (const t of [byId("mx"), byId("viol"), byId("feat")]) {
	const strips = [0, 1].map(() => t.appendChild(Object.assign(document.createElement("div"), {className: "hl"})));
	const off = () => { for (const s of strips) s.style.display = "none"; };
	t.addEventListener("mouseover", e => {
		const c = e.target.closest("td,th");
		if (!c || !t.contains(c)) return off();
		const row = c.parentElement, m = t.getBoundingClientRect(), r = row.getBoundingClientRect(), b = c.getBoundingClientRect();
		const x = m.left + t.clientLeft, y = m.top + t.clientTop;
		const onRow = c.closest("thead") === null && !c.classList.contains("ed");
		const onCol = c.tagName == "TD" ? c !== row.firstElementChild : c.hasAttribute("title") || c.hasAttribute("data-k");
		strips[0].style.cssText = onRow ? `display:block;left:0;top:${r.top - y}px;width:${t.clientWidth}px;height:${r.height}px` : "";
		strips[1].style.cssText = onCol ? `display:block;top:0;left:${b.left - x}px;height:${t.clientHeight}px;width:${b.width}px` : "";
	});
	t.addEventListener("mouseleave", off);
}
"""


def write(report: dict, path: Path) -> None:
	# render() closes every element: minify-html (0.16 to 0.18 at least) drops a </table> that follows an
	# implicitly closed cell, which pulls the rest of the page into the table.
	path.write_text(minify_html.minify(render(report), minify_css=True, keep_closing_tags=False))


if __name__ == "__main__":
	src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("out/report.json")
	dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".html")
	write(json.loads(src.read_text()), dst)
