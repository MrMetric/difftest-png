# Decisions

Why difftest-png tests, judges and reports the way it does. If you want to change one of these
rules, start here.

## Scope

The report is meant to help someone choose a PNG library, or find out how far they can trust
the one they already use. Bug reports to upstream projects are out of scope, though the hope is
that showing the mess plainly helps get some of it cleaned up.

Only decoders are tested for now. APNG support comes next, then colour management, then
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
the image's own colour space and then goes through whatever colour management every other
pixel gets. The text says "displaying the pixel as opaque black", but display black regardless
of the profile would mean tracking the pixel through the colour pipeline, which no decoder
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

The suite holds every conformance test plus every file that any implementation fails. For each
test, each lens says what a decoder is to do with the file: display it, recover from its fault,
accept or reject it as it likes, or reject it. Going against that is a "must" violation where
the specification requires and a "should" violation where it recommends. The filename prefix
(`y_`, `n_`, `r_`, `i_`, borrowed from JSONTestSuite) summarizes the third edition's answer. A file has one fault; a file with two faults would be
two tests. A behaviour that breaks a different set of decoders gets its own test, which is why
tRNS has a test for each bit depth.

The test files are committed as they are, alongside `decode/manifest.tsv`. No generator lives in
the repo. If you edit a file by hand, `expect.py` recomputes its expectations from the reference
decoder. The reference also records which spec clauses each file exercises, and `run.py`
refuses to run if a file stops exercising one. That catches a test quietly breaking, for
instance when an optimizer is run over the suite.

Test files should be as small as they can be while still testing what they're for. Minimizing
happens once, after a full run, so every shrunken file can be checked against its original's
results. The current suite isn't minimized yet. Optimizers like `oxipng` are only safe where
they can't change what a file tests.

Files from third-party test suites are included. A file from a suite whose licence forbids
redistribution only matters if it is more than an obvious, minimal example of its case, and
since the tests are about representation, its pixels can be swapped for others. Attribution
still needs writing.

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
separate pass. Valgrind works on unmodified binaries and catches reads of uninitialised memory,
which AddressSanitizer misses.

Each decoder is driven the way its documentation says to use it. If that still can't produce
correct output, the report shows it as a `shim` failure.

A shim measures the decoder, not itself. It holds one row and writes rows out as the decoder
produces them, so it can't run out of memory where the decoder wouldn't. A decoder whose API
only hands over whole images allocates them itself, and that is its own result. An error the
decoder raises after the last row counts as an error like any other. Whether the rows already
decoded are good enough is the application's decision, and the shim doesn't make it.

Some differences are recorded without counting as failures. The colour under fully transparent
pixels may be preserved, zeroed or changed to something else. The last case is the surprising
one, and exactly what this kind of testing can catch. Decoders that only produce 8 bits may
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

Every run is fresh. Stored results from earlier runs are only for development, and only used
when asked for with `--reuse`; `--rerun` names the decoders to run again. Nothing tries to
detect on its own whether the machine has changed.

Decoders get 60 seconds per file, and 120 under valgrind. A decoder that times out is reported
as hanging, without being retried. If valgrind fails for any other reason, the whole run stops
with an error, since that's a problem with the test machine rather than a result. Large runs
are memory-capped so a runaway decoder can't take down the rest of the system.

## The report

The report is one minified HTML file with no JavaScript, in a dark theme. Implementations are
sorted by their number of must-violations, then should-violations, under the third edition. There is no overall score,
because choosing weights for one would be an editorial judgement. A decoder that passes every
test only means the suite needs more tests.

In the matrix, each cell is a single coloured symbol, so the whole table can be taken in at a
glance. Cells have no tooltips. Implementations with identical results share a column in the
HTML, but stay separate in the JSON.

The human disclaimer is written in the README, and the report copies it from there.

## Still open

- Recording documented deviations.
- A shim audit: each shim relays its library's diagnostic channel, or the column records that the
  library has none, so a silent accept can be read as the library's choice rather than the shim's.
- Attribution for third-party test files, and a licence for the repo.
- A script to clone and build upstream development versions, possibly with sanitizer builds.
- Minimizing the suite.
- APNG frames, colour management and encoders.
- Whether Apple's ImageIO treats the row before each iDOT segment as zeros, the way the row
  before the image's first row is defined. That would be a bug: a file written that way decodes
  to the wrong pixels in every ordinary decoder. But it's an easy mistake to make, so ImageIO is
  worth checking.
- A colour management test for the out-of-range palette index: an indexed image with such an
  index and an ICC profile that maps black elsewhere, to show where the substitution happens.
- The fourth edition, now a W3C editor's draft: what it changes for decoders, and whether it
  should get its own set of expectations.
