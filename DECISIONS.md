# Decisions

Why difftest-png tests, judges and reports the way it does. If you want to change one of these
rules, start here.

## Scope

The report is meant to help someone choose a PNG library, or find out how far they can trust
the one they already use. Bug reports to upstream projects are out of scope.

Only decoders are tested for now. APNG support comes next, then colour management, then
encoders, which will get their own `encode/` directory. Performance isn't measured at all: the
decoders disagree about correctness often enough that speed would be a distraction. The report
lists what it doesn't test.

## Editions

Two editions of the specification matter: the second (ISO/IEC 15948:2003) and the third (W3C,
2025). PNG 1.0 and 1.1 count as the second.

Following the older edition is fine. Following one edition on some tests and the other on
others is what the report calls "mixed". The label describes what a decoder does, whatever its
authors intended. Only places where the editions contradict each other count towards it. The
third edition also adds features (APNG, cICP, eXIf), and supporting them doesn't make a decoder
mixed.

There is one such contradiction. For a palette index past the end of PLTE, the second edition
requires rejecting the file, and the third requires rendering the pixel opaque black.

tRNS and bKGD values with bits set above the image's bit depth look like a second contradiction,
but they aren't. The second edition says "the least significant bits are used", which already
tells the decoder to mask the value. The third edition just says so more clearly. A decoder
that doesn't mask has a bug under both.

A decoder may document a deviation on purpose, such as skipping CRC checks for speed. That
should be recorded as a choice rather than counted as a bug. Nothing records it yet, but the
design has to leave room for it.

## The test suite

The suite holds every conformance test plus every file that any implementation fails. Each
test lists the outcomes it allows under each edition, and how serious each one is: a pass, a
"should" violation or a "must" violation. The filename prefix (`y_`, `n_`, `r_`, `i_`, borrowed
from JSONTestSuite) summarizes that list. A file has one fault; a file with two faults would be
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

## The reference decoder

`refpng.py` is written from the specification, with its own inflater (`inflate.py`) so that
zlib's quirks can't leak into the expected results. It is a column in the report like any other
decoder. Every check names a spec clause and quotes the sentence it enforces, much like the
Vulkan validation layer. It has to be small, readable and impossible to get subtly wrong.
Performance doesn't matter much.

"Impossible to get subtly wrong" turned out to mean something specific. A rule that applies
across chunks has to be enforced in one place that every path goes through. Otherwise it gets
forgotten in some handler, and tests won't notice. So chunk layouts are declared once and parsed
by one parser. Faults are recorded one way and reported on every exit path. A bad field value
records a fault and parsing carries on; only a broken layout ends the chunk. And a fault's
severity has to match its clause, which is asserted.

## Judging

Warnings never count against a decoder. Crashes, hangs and memory errors are must-violations.

So is running out of memory. A decoder fed a hostile file should return an error. If it
exhausts memory instead, any program that opens untrusted images can be taken down by one
file. This applies even when the decoder is killed by the test run's own memory limit, because
no decoder should need that much memory; a machine with more RAM would only hide the problem.

Memory errors are found by running each native-code decoder under valgrind memcheck, in a
separate pass. Valgrind works on unmodified binaries and catches reads of uninitialised memory,
which AddressSanitizer misses.

Each decoder is driven the way its documentation says to use it. If that still can't produce
correct output, the report shows it as a `shim` failure.

Some differences are recorded without counting as failures. The colour under fully transparent
pixels may be preserved, zeroed or changed to something else. The last case is the surprising
one, and exactly what this kind of testing can catch. Decoders that only produce 8 bits may
reduce 16-bit samples by taking the high byte or by rounding. Palette and sub-byte expansion
must still be exact.

Some files allow more than one correct result:

- An ancillary chunk with a bad CRC may be dropped, or used anyway (✓ᶜ), since skipping CRC
  checks is a legitimate tradeoff.
- With several tRNS chunks, keeping either the first or the last (✓ᵗ) is coherent. Any other
  pixels are wrong. Rejecting the file is a should-violation, since the spec asks decoders to
  recover.
- If the compressed image data is corrupt only after the last row, decoders may accept or
  reject. Some stop inflating once they have every row, and never see the damage. A wrong
  Adler-32 checksum is still fatal.

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

Apple's iDOT chunk was never registered, and its uppercase second letter claims a public name
it has no right to. Strictly, that makes the file non-conforming. But the spec doesn't respect
the private bit itself: the APNG chunks it standardized all have lowercase second letters. And
Apple has published a great many PNG files carrying iDOT that work fine. So a file with iDOT
counts as conforming. iDOT exists only to speed up decoding, so a file must always decode to
the same pixels as it would without the chunk. A decoder that rejects such a file, as Apple's
ImageIO sometimes does, has a bug.

Validators like pngcheck are judged on whether the file conforms to the specification, which
isn't the same as whether a decoder must decode it. A chunk name with the reserved bit set is
the clearest case: decoders must ignore the chunk and show the image, but the spec says the
file doesn't conform, and a validator should say so.

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
sorted by their number of must-violations, then should-violations. There is no overall score,
because choosing weights for one would be an editorial judgement. A decoder that passes every
test only means the suite needs more tests.

In the matrix, each cell is a single coloured symbol, so the whole table can be taken in at a
glance. Cells have no tooltips. Implementations with identical results share a column in the
HTML, but stay separate in the JSON.

The human disclaimer is written in the README, and the report copies it from there.

## Still open

- Recording documented deviations.
- Attribution for third-party test files, and a licence for the repo.
- A script to clone and build upstream development versions, possibly with sanitizer builds.
- Minimizing the suite.
- APNG frames, colour management and encoders.
- The fourth edition, now a W3C editor's draft: what it changes for decoders, and whether it
  should get its own set of expectations.
