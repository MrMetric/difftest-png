# Decisions

Why difftest-png tests, judges and reports the way it does. If you want to change one of these
rules, start here.

## Scope

The report is meant to help someone choose a PNG library, or find out how far they can trust
the one they already use. Bug reports to upstream projects are out of scope, though the hope is
that showing the mess plainly helps get some of it cleaned up.

Only decoders are tested for now. APNG support comes next, then color management, then
encoders, which will get their own `encode/` directory. Performance isn't measured at all: the
decoders disagree about correctness often enough that speed would be a distraction. The report
lists what it doesn't test.

## Editions

Two editions of the specification matter: the second (ISO/IEC 15948:2003) and the third (W3C,
2025). PNG 1.0 and 1.1 count as the second.

Every result is rated under each edition separately, and the report shows both ratings side by
side. There is no single verdict and no "mixed" label: following one edition rather than the
other isn't a defect, and which one a reader needs is their call, not the report's. The two
ratings differ only where the texts contradict each other.

A rating is made through a lens: an edition's text plus the extensions. Under the second
edition, the chunks the third edition added (APNG, cICP, mDCV, cLLI, eXIf) are third-party
extensions with their own rules, like the W3C registered extensions and Apple's iDOT.
Supporting them is second-edition compliance with extensions, not a departure from it, and a
decoder that mishandles one is rated the same way under both lenses.

There is one contradiction. For a palette index past the end of PLTE, the second edition
requires rejecting the file, and the third requires rendering the pixel opaque black. The
substitution happens where the index is resolved, at palette lookup, so the pixel is black in
the image's own color space and then goes through whatever color management every other
pixel gets. The text says "displaying the pixel as opaque black", but display black regardless
of the profile would mean tracking the pixel through the color pipeline, which no decoder
does, and a target that cannot show black would still have to show something. tRNS can't be
longer than PLTE, so such a pixel is opaque under every reading.

tRNS and bKGD values with bits set above the image's bit depth look like a second contradiction,
but they aren't. The second edition says "the least significant bits are used", which already
tells the decoder to mask the value. The third edition just says so more clearly. A decoder
that doesn't mask has a bug under both.

A decoder may document a deviation on purpose, such as skipping CRC checks for speed. That
should be recorded as a choice rather than counted as a bug. Nothing records it yet, but the
design has to leave room for it.

## The test suite

The suite is generated: every test is a file `synth.py` writes, and nothing else. For each test,
each lens says what a decoder is to do with the file: display it, recover from its fault,
accept or reject it as it likes, or reject it. Going against that is a "must" violation where
the specification requires and a "should" violation where it recommends. The filename prefix
(`y_`, `n_`, `r_`, `i_`, borrowed from JSONTestSuite) summarizes the third edition's answer. A file has one fault; a file with two faults would be
two tests. A behavior that breaks a different set of decoders gets its own test, which is why
tRNS has a test for each bit depth.

Every test shows one picture. `synth.py` generates them from `picture.py`, which
defines a 13 by 15 pixel scene once, with samples in [0, 1], and derives it to every color type
and bit depth. Unless a test is about something else, its file is that picture at 13 by 15, RGB,
8 bits, not interlaced, filter None, zlib-compressed. A test about grayscale uses the grayscale
derivation, one about an awkward Adam7 shape uses the top-left crop, and so on. The tests then
differ only in what they test, and their decoded images are directly comparable: usually
identical, otherwise the same scene within what the color type and depth can hold, so a reader
can tell at a glance whether a decoder showed the right thing.

The picture is built so that the representation tests have something to bite on: gradients with
a different slope in each channel, a flat region with a soft edge, rows that repeat, pure black
and pure white at every depth, and an alpha fade whose transparent pixels keep their color. 13 is
the smallest width and height at which every Adam7 pass is at least two pixels in each direction
and every sub-byte depth leaves padding bits in a row; the height is 15 so that the picture isn't
square and a swapped width and height shows. The generator checks each file against the reference
before writing it: the lens-3 expectation, the clause the test is about, and the pixels. A test
that doesn't exercise what it claims, or whose pixels the generator can't predict, is refused.

The test files are committed as they are, alongside `decode/manifest.tsv`, so the suite runs
without the generator and a file can be edited by hand without regenerating. If you
edit a file by hand, `expect.py` recomputes its expectations from the reference decoder. The
reference also records which spec clauses each file exercises, and `run.py` refuses to run if a
file stops exercising one. That catches a test quietly breaking, for instance when an optimizer is
run over the suite.

The third-party test suites (PngSuite, the browsers' and the libraries' own) were mined once, at the
start: every implementation was run over them, and each file some implementation failed was read for
what tripped the decoder, which is now a generated case on the picture. None of their files is in the
suite, so there is nothing to attribute or relicense, and their pixels never mattered, since the tests
are about representation. A new third-party file is a lead for a new generated case, not a test.

The picture sets the size of the files: 13 by 15 is the smallest that exercises every Adam7
pass and every padding case, so the files are as small as they can be while still showing it.
The exceptions are the tests whose point is size (the dimension cases, the large text chunks,
the decompression bombs). Lossless optimizers like `oxipng` are only safe where they can't
change what a file tests, and for most generated files the representation is exactly what is
tested, so the generator writes each file at its size and nothing rewrites it afterwards.

## The reference

The reference has two jobs that stay apart: decoding and judging.

`refpng.py` decodes. It is written from the specification, with its own inflater (`inflate.py`)
so that zlib's quirks can't leak into the expected results. It knows nothing about editions. From one parse it produces every
interpretation of the file's pixels that the specification or its recovery advice defines, each
named by the choices that produced it (the first or the last of two tRNS chunks, a chunk with a
bad CRC dropped or used as stored, an unknown critical chunk ignored), and it records every
fault and every clause the file reaches. It gives up only when no pixels can exist: no usable
IHDR, no image data, image data that cannot be inflated to the last row. Everything else is
decoded and recorded, because whether a decoder ought to have stopped is not the decoder's
question.

`pnglens.py` judges. Given the record of faults and a lens, it says whether the file conforms,
lists the faults in words, says what the specification expects a decoder to do with the file,
and rates each interpretation as recommended or arguable. Validators are judged on its
conformance verdict, decoders on its expectation.

The reference appears in the report as two columns, one per lens: the decoder with that lens's
verdict applied, rejecting what the lens says must be rejected and otherwise writing its
best-rated interpretation. Each passes every test under its own lens by construction, and the
run fails if it doesn't. Under the other lens it shows exactly where the editions contradict
each other, which is the one place they do.

Every check names a spec clause and quotes the sentence it enforces, much like the Vulkan
validation layer. The reference has to be small, readable and impossible to get subtly wrong.
Performance doesn't matter much.

"Impossible to get subtly wrong" turned out to mean something specific. A rule that applies
across chunks has to be enforced in one place that every path goes through. Otherwise it gets
forgotten in some handler, and tests won't notice. So chunk layouts are declared once and parsed
by one parser. Faults are recorded one way and reported on every exit path. A bad field value
records a fault and parsing carries on; only a broken layout ends the chunk. And a fault's
severity has to match its clause, which is asserted.

## Judging

Warnings never count against a decoder. Crashes, hangs and memory errors are must-violations.

Silence is recorded. A decoder that accepts a non-conforming file and says nothing is doing
something different from one that accepts it with a diagnostic, and the report shows which it was:
each column's count of silent accepts under each lens, next to its violations. It has no severity,
since what to make of it is the reader's call.

So is running out of memory. A decoder fed a hostile file should return an error. If it
exhausts memory instead, any program that opens untrusted images can be taken down by one
file. This applies even when the decoder is killed by the test run's own memory limit, because
no decoder should need that much memory; a machine with more RAM would only hide the problem.

Memory errors are found by running each native-code decoder under valgrind memcheck, in a
separate pass. Valgrind works on unmodified binaries and catches reads of uninitialized memory,
which AddressSanitizer misses.

Each decoder is driven the way its documentation says to use it.

A library that returns without error but without an image gets an `api` result, a
must-violation under both lenses whatever the file permits: too few rows, a frame that is
not the canvas, an empty image for a zero width, `nil` where a pixel should be. The library
did not reject the file, so the shim does not report a rejection; it reports what was
missing. A failure of the shim itself is `shim`: not scored, listed under the run's problems,
and fixed.

A shim measures the decoder, not itself. It holds a bounded band of rows, never the image,
and writes rows out as the decoder produces them. It applies nothing the library hands back
as data, such as a transparency key, so what the library left undone shows in the results. A decoder whose API only hands over whole
images allocates them itself, and that is its own result. An error the decoder raises after
the last row counts as an error like any other. The shim takes the step after the rows that
the library's own whole-image call takes, and no other: libpng's reads to the end of the
datastream, libspng's and the png crate's do not. Whether the rows already decoded are good enough is
the application's decision, and the shim doesn't make it.

Each column records its library's diagnostic channel, or that it has none, so a silent
accept reads as the library's choice rather than the shim's.

An APNG is decoded the way any PNG is. A library whose plain path yields an animation
frame instead of the default image shows that in its results; the shim does not steer it.

Some differences are recorded without counting as failures. The color under fully transparent
pixels may be preserved, zeroed or changed to something else. The last case is the surprising
one, and exactly what this kind of testing can catch. The summary reports it per source of the
transparency, an alpha channel, a tRNS color key or a tRNS palette, because a decoder that expands
a color key itself may zero the color there while it preserves what an alpha channel or a palette
carries. Decoders that only produce 8 bits may
reduce 16-bit samples by taking the high byte or by rounding. Palette and sub-byte expansion
must still be exact.

Each implementation's output is an observation. Outputs that are identical pixel for pixel
share a bucket, and the report shows, per test, how the implementations distribute over the
buckets. A lens labels a bucket that matches one of the reference's interpretations: expected
when the interpretation's choices are the recommended ones, arguable otherwise. A bucket the
reference does not produce is wrong, and a populous one is a reason to look at the reference
first. The same holds in the one place the editions contradict each other: under the third
edition the opaque-black bucket is the expected one, under the second it is an accepted
invalid file.

Some files allow more than one coherent result, and the report shows which one each decoder
chose rather than ruling:

- An ancillary chunk with a bad CRC may be dropped, which the specification recommends, or
  used anyway, since skipping CRC checks is a legitimate tradeoff.
- With several tRNS chunks, keeping either the first or the last is coherent. Any other
  pixels are wrong. Rejecting the file is a should-violation, since the spec asks decoders to
  recover.
- If the compressed image data is corrupt only after the last row, decoders may accept or
  reject. Some stop inflating once they have every row, and never see the damage. A wrong
  Adler-32 checksum is still fatal.

Image data that ends before the last row has no reference pixels. The specification says an
unexpected end of stream may be fatal, so accepting and rejecting both pass, and what a decoder
shows for such a file is reported without being judged.

A DEFLATE stream that declares 31 or 32 distance codes must be decoded. Only 30 codes are
defined, and zlib rejects such streams, so this looks like a matter of implementation choice.
But RFC 1951 §3.3 says "a compliant decompressor must accept the full range of possible
values", and that range goes up to 32. Every decoder built on zlib fails this test.

A chunk's type and position are checked before its CRC, so a bad CRC can't hide a second
IHDR.

cICP's fields take their values from ITU-T H.273 (07/2024), which says its reserved code points
"shall not be used". A cICP chunk that uses one has an unexpected value in a known ancillary
chunk. Section 13.2 recommends ignoring such a chunk but doesn't require it, so a decoder may
reject the file, though it shouldn't. A decoder that shows the image must produce the same
pixels as it would for the file without the cICP chunk.

Apple's iDOT chunk is an extension. It was never registered, and its uppercase second letter
claims a public name, which the naming rules alone would make a fault. An extension's
definition takes precedence over the default it collides with, so a file with iDOT conforms
under both lenses, and a validator that flags the name anyway is recorded as strict rather than
wrong. iDOT exists only to speed up decoding, so a file must always decode to the same pixels
as it would without the chunk. A decoder that rejects such a file, as Apple's ImageIO sometimes
does, has a bug.

Otherwise the naming rules hold. A file with a public chunk that neither the spec, the
registry nor a known extension defines doesn't conform, and neither does a chunk name with the
reserved bit set.

eXIf is an extension under the second edition, registered in 2017, and part of the text in the
third. Both lenses treat it the same.

An iDOT chunk only conforms if it actually works. Its segments have to cover the image's rows in
order. Each offset has to point at the IDAT chunk where its segment starts. And each segment has
to be decodable on its own: its compressed data starts on a byte-aligned block boundary, nothing
after that point refers back to data before it, and its first row isn't filtered against the
previous segment's last row. A stale iDOT is the most common way this goes wrong, for instance
after a tool rewrites the IDAT chunks. A validator that checks iDOT at all should flag it.

Validators like pngcheck are judged on whether the file conforms to the specification, which
isn't the same as whether a decoder must decode it. A chunk name with the reserved bit set is
the clearest case: the file doesn't conform, and a validator should say so, but a decoder
treats the chunk as unknown and shows the image. Section 15.3.3 makes that a condition of
decoder conformance, so a decoder that rejects the file is non-conforming, whatever 13.5's
"should not flag an error" might suggest about the severity.

## Implementations

Libraries and programs built on libpng (ImageMagick, GraphicsMagick, OpenCV, pngtopam) are
tested too, since people use them directly. The report shows what each is built on. Releases
and upstream development versions are separate columns. libpng is tested on both of its
branches, libpng16 and libpng18. Programs that link libpng use whichever copy they normally
would.

A decoder whose program isn't installed is skipped and reported as missing. The runner tells
this from the command's own failure to run (exit status 127, or a program that can't be
executed).

## Runner

`run.py` uses Python through `uv`, the standard library where possible, and well-known
libraries otherwise. By default it tests every implementation it finds on the machine. It's
Linux only for now.

Every run is fresh. Stored results from earlier runs are only for development, only used when
asked for with `--reuse`, and then only for the same program: a result is keyed on the program
that produced it, its version string, revision, files and shared libraries, so a rebuilt or
upgraded decoder runs afresh. `--rerun` names the decoders to run again regardless, for a change
the key does not see.

Decoders get 60 seconds per file, and 120 under valgrind. A decoder that times out is reported
as hanging, without being retried. If valgrind fails for any other reason, the whole run stops
with an error, since that's a problem with the test machine rather than a result. Large runs
are memory-capped so a runaway decoder can't take down the rest of the system.

## The report

The report is one HTML file with everything inline, in a dark theme. It reads completely without
JavaScript: the edition checkboxes and the filter to the tests some implementation fails are CSS.
The script adds what CSS can't: sorting columns, hiding columns, filtering rows by text,
expectation class and family, and picking one column, by its header in any of the three
matrix-shaped tables, to see what it fails. Tables do not reach into each other: each orders its
own columns (the matrix by name or violations from its corner, the violations table by a clicked
row, the implementations table by a clicked header), and the picked column is the one shared
state, marked in every table, each of which narrows its own rows to it: the matrix to the tests
it fails, the family table to the families it fails in, the violations table to its rows above 0.
Nothing scrolls the page. One line above each table says what a click there does.

Each table has its own checkbox per edition and shows the ticked ones, so a reader can flip
editions where the difference is. The violations and family tables show a block of rows per
edition, labelled down the side. The matrix keeps one row per test and splits a cell into one
glyph per edition where the editions disagree, because only a handful of files differ and a second
row per test would bury them.

The matrix's rows are CSS grids with content-visibility, not table rows, under the table markup.
Re-sorting or hiding its columns then changes a few CSS variables instead of moving thousands of
cells, and the browser lays out only the rows on screen, which makes the change immediate.

Columns come in three kinds, the reference, validators and decoders, in that order, with a
heavier border between them. A validator's accept and a decoder's correct pixels are different
results and should not sit side by side looking alike, and a validator's verdict on the file is
context for reading the decoders' results, so it comes first, where a left-to-right reader meets
it before them. Within a kind the columns are alphabetical, where a reader looking for one library
expects it; the matrix can re-sort them by violations, the violations table by any of its rows. There is no
overall score, because choosing weights for one would be an editorial judgement. A decoder that
passes every test only means the suite needs more tests.

In the matrix, each cell is a single colored symbol, so the whole table can be taken in at a
glance. Cells have no tooltips. Implementations with identical results share a column in the
HTML, but stay separate in the JSON. The rows are a flat list in the suite's order, and the
filters do whatever grouping a reader wants.

A test's expansion explains the file rather than quoting the rule it breaks. It opens with a
strip of the file's chunks, color-coded, marking a bad CRC, a truncated chunk and anything after
IEND, so the text never has to describe the structure. Then one caption from the generator says
what the file is, and one sentence per clause says what the specification makes of it, with the
section linked. Those sentences are written once per clause in `spec/clauses.tsv`, in plain
words; the reference's own fault record stays in the JSON. After that come the expectation under
each lens, links to the sections the clauses cite, and every output: the expected pixels, then
each bucket of identical decoder outputs as a thumbnail with the columns that produced it. The
expected bucket's columns go unnamed while it outnumbers every other bucket, so that the names
listed are the exceptions; where another bucket has as many, it names them too. A
wrong output is shown, not only counted. Its label says how it differs from the expected pixels,
and a difference needs no further comment to read as wrong. Where the generator knows a misreading
that produces exactly those pixels, an invalid filter byte taken as filter None for instance, the
label names the misreading instead.

A violations table, shaped like the matrix, splits must-violations by what went wrong: a valid
file rejected, an invalid file accepted, wrong pixels, or a crash, hang, memory error or shim
failure. The properties that are not violations, the color under alpha 0 and the reduction of
16-bit samples, are two sentences naming the exceptions, since nearly every decoder does the same.
A family table rolls the results up per part of the suite.

The human disclaimer is written in the README, and the report copies it from there.

## Still open

- Recording documented deviations.
- A license for the repo.
- A script to clone and build upstream development versions, possibly with sanitizer builds.
- APNG frames, color management and encoders.
- Whether Apple's ImageIO treats the row before each iDOT segment as zeros, the way the row
  before the image's first row is defined. That would be a bug: a file written that way decodes
  to the wrong pixels in every ordinary decoder. But it's an easy mistake to make, so ImageIO is
  worth checking.
- A color management test for the out-of-range palette index: an indexed image with such an
  index and an ICC profile that maps black elsewhere, to show where the substitution happens.
- The fourth edition, now a W3C editor's draft: what it changes for decoders, and whether it
  should get its own set of expectations.
