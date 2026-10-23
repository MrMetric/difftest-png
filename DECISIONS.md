# Decisions

The design decisions behind difftest-png and the reasons for them. A change to how tests are
chosen, judged or reported should start here.

## Goal and scope

- **The report answers "Which PNG library should I use?" and "How reliable is my software?"**,
  and nothing else. Filing upstream bug reports is not part of it.
- **Decoders first.** Encoder tests come later, in `encode/` beside `decode/`. After decoders,
  the next priority is full APNG support, then colour management, then encoders.
- **Correctness, not performance.** The report has no timing or memory figures; the state of
  decoder correctness matters far more.
- **Not tested, and the report says so:** animation frames after the default image, colour
  management, encoders, performance. There are no per-finding write-ups; the matrix is the
  result.
- **Knowing what is not necessary matters as much as what is.** Machinery nobody asked for is
  left out, even when it would make something automatic.

## Editions of the specification

- **Only the second edition (ISO/IEC 15948:2003) and the third (W3C, 2025) matter.** PNG 1.0
  and 1.1 fold into the second.
- **Following an older edition is legitimate; following different editions on different tests
  is flagged as mixed.** Mixed describes behaviour, not intent: intent doesn't change what a
  user of the decoder gets.
- **Only contradictions between the editions decide which edition a decoder follows.** Features
  that the third edition adds (APNG, cICP, eXIf) never make a decoder mixed.
- **The reference decoder takes the edition as a parameter** and appears as two columns.
- **A palette index past the end of PLTE is the one contradiction.** The second edition calls it
  an error, so the file must be rejected; the third requires rendering it opaque black.
- **tRNS and bKGD values with bits above the bit depth are masked under both editions.** The
  second edition's "the least significant bits are used" is an instruction to the decoder, and
  the third edition only makes it explicit. A decoder that doesn't mask has a bug; it is not
  following the older edition.
- **Room for documented behaviour.** A decoder may document a deviation as a deliberate
  tradeoff (e.g. skipping CRC checks for speed). That is a tradeoff, not a bug, and the design
  must leave room to record it. Nothing records it yet.

## The test suite

- **Which files are in the suite: every conformance test, plus every file any implementation
  fails.**
- **Each test file should be as small as it can be without defeating its purpose.** Minimizing
  happens once, after a full run, so every minimized file can be checked against its original's
  results. It is optional; the current suite is not minimized.
- **Optimizers may shrink a file only where they cannot change what it tests.** Tools like
  `oxipng` can make reductions that break a test.
- **The files themselves are the source of truth**, committed with `decode/manifest.tsv`. No
  generator lives in the repo; after a hand edit, `expect.py` recomputes expectations from the
  reference.
- **Each test lists its allowed outcomes per edition, each with a severity** (pass, should,
  must). The filename prefix (`y_`, `n_`, `r_`, `i_`, as in JSONTestSuite) only summarizes the
  list, since many cases have several acceptable outcomes and several wrong ones.
- **A behaviour that breaks a different set of decoders is a separate test**, e.g. tRNS at each
  bit depth.
- **A test has one fault.** Two faults in one file are two tests.
- **The reference records which spec clauses each file reaches, so the corpus tests itself.**
  `run.py` fails if a file stops reaching one, which catches silent breakage such as a careless
  optimizer run over the suite.
- **Third-party test files are shipped.** A file from a corpus whose licence forbids
  redistribution needs attention only if it is more than an obvious, minimal example of its
  case; representation is what's tested, so pixel content can be replaced. Attribution is still
  to do.
- **Dedicated alpha-0 tests** have fully transparent pixels holding distinct non-zero colours.

## The reference decoder

- **It must be rock-solid**: minimal, comprehensible, and with no room for errors to creep in.
  Every spec clause is checked explicitly and named in comments and messages, in the spirit of
  the Vulkan validation layer. Performance is secondary, though that is no excuse for waste.
- **It is a column in the comparison.**
- **It is written from the specification**, as `refpng.py`. Every check carries a stable clause
  id with the normative sentence quoted beside it.
- **It has its own inflater and Adler-32** (`inflate.py`), for the same reason as the PNG model:
  inflate must not be a black box with its own quirks.
- **Rock-solid means correct by construction.** A rule that applies across chunks is enforced in
  one place every path goes through, so the class of mistake can't be written. Code that merely
  passes its tests is not enough. Concretely:
  - chunk layouts are declared once and parsed generically;
  - faults are recorded in one way;
  - every exit path reports them;
  - a bad value records a fault and parsing continues; only a broken layout ends the chunk;
  - a fault's severity must match its clause's consequence (asserted).

## Judging results

- **Warnings never count against a decoder. Crashes, hangs and memory errors are always
  must-violations.**
- **Running out of memory is a must-violation**, whatever the machine. A decoder should reject
  a file with an error, not exhaust its host; for any program that opens untrusted images, that
  is a denial of service. This holds when the run's own memory cap kills a decoder too: no
  decoder should need that much, and more RAM would only hide it. The report shows
  out-of-memory separately from a crash.
- **Memory errors are found with valgrind memcheck**, in a separate pass over the native-code
  columns. A sanitizer pass is more reliable than hoping a bug shows itself; valgrind runs on
  unmodified binaries and catches uninitialised reads, which ASan doesn't. ASan and UBSan builds
  can come with upstream builds.
- **Shims use each library as its documentation says to.** If a shim can't make the library
  produce conforming output through its documented interface, that is a result (`shim`), not
  N/A.
- **Colour under fully transparent pixels doesn't fail a test.** Whether a decoder preserves,
  zeroes or alters it is recorded as a property. Altered is the surprising case, and exactly
  what this kind of testing exists to catch.
- **16-bit images on 8-bit-only decoders:** taking the high byte and rounding both pass, and the
  method is recorded as a property. Palette and sub-byte expansion must be exact.
- **An ancillary chunk with a bad CRC:** decoding with the chunk used as stored also passes
  (✓ᶜ), since skipping CRC checks is a tradeoff.
- **Several tRNS chunks:** keeping the last one is also coherent, so it passes (✓ᵗ), and the
  matrix shows which case matched. Any other pixels are wrong. Rejecting is a should-violation,
  since the spec says to recover.
- **HDIST of 31 or 32 must be accepted.** RFC 1951 §3.3: "A compliant decompressor must accept
  the full range of possible values". The DEFLATE format defines only 30 distance codes, which
  makes the case look implementation-defined. But zlib's rejection is a deviation from the RFC,
  and a widespread bug is still a bug.
- **Corrupt compressed data after the image is complete:** either outcome passes. Some decoders
  stop inflating once they have every row. It's a notable difference, but not a failure. A wrong
  Adler-32 stays fatal.
- **Chunk type and position rules are checked before the CRC**, so a bad CRC can't hide a
  second IHDR.
- **Validators are judged on conformance.** A validator such as pngcheck serves the same purpose
  as the reference decoder: it should accept exactly the files that conform to the
  specification. That is a different question from what a decoder must do. A decoder must
  decode a file with a reserved-bit chunk name, but the file doesn't conform, and a validator
  should say so.

## Implementations

- **Every implementation readers are likely to use.** Programs built on libpng (ImageMagick,
  GraphicsMagick, OpenCV, pngtopam) are columns too, because readers use them directly. Lineage
  is shown.
- **Release and HEAD are separate columns:** each version is, in effect, a different
  implementation.
- **libpng is tested on both branches** (libpng16 and libpng18). Programs built on it use
  whichever libpng they prefer, system or bundled.
- **The published run includes upstream development revisions.** A script that clones and builds
  them isn't a goal yet, but testing again later must be easy.
- **A missing program is its own case, never a result.** The runner checks directly whether the
  command ran (exit status 127, or a program that can't be executed), rather than inferring it
  from a probe decode.

## Runner

- **Python via `uv`, tabs, the standard library or well-regarded libraries.** Nothing is
  hand-written that a single `import` solves.
- **It runs every implementation present on the machine by default**, and any can be named.
  Linux only for now.
- **Report data is JSON**, tab-indented and readable where that costs nothing. The HTML is
  generated from it automatically.
- **Every run is fresh. Stored results are a development aid**, behind explicit flags (`--reuse`,
  `--rerun`). Nothing fingerprints the system automatically: a machine's state doesn't change at
  random, and adversarial changes are out of scope.
- **Decoders read each test file at its real path**, and stored results key on the full path.
- **Time limits:** 60 s per run, the same for every decoder; a PNG decoder shouldn't take that
  long. There is no confirmation pass for timeouts, since a timeout is flagged either way. Under
  valgrind the limit is a fixed 120 s, and valgrind runs without network access (no
  debuginfod).
- **A valgrind timeout is a timeout result. Valgrind failing to finish otherwise is a runner
  error that stops the run**, not a cell state.
- **Heavy runs are memory-capped**, because the kernel's OOM killer often kills the wrong
  process or nothing at all.
- **Stored decoded images are compressed with zstd**, from the Python 3.14 standard library.

## The report

- **One self-contained, minified HTML page.**
- **Dark theme only, square borders, no JavaScript** where modern web standards suffice. Full
  page width.
- **Counts, not scores.** Implementations are sorted by must-violations, then
  should-violations, with lineage shown. Counts and lineage are ground truth; a composite score
  would be an editorial judgement that adds confusion and removes none. The report counts
  errors and never says one decoder "scores worse" than another.
- **A decoder that passes everything means the suite needs more tests**, not that the decoder is
  perfect.
- **The matrix:** tests are grouped by spec section, one coloured glyph per cell, details in
  `<details>`, a filter for failing rows, no images and no cell tooltips. The point is a
  colour-coded matrix the eye can take in at once.
- **Columns with identical results merge in the HTML only**; the JSON keeps them apart.
- **A properties row** for each implementation gives its alpha-0 colour handling, 16→8
  reduction and edition verdict.
- **Room for APNG:** the shim contract documents how output generalizes to frames; nothing is
  built yet.
- **The human disclaimer lives only in the README.** `report.py` renders it as CommonMark,
  verbatim.

## Still open

- How documented deviations will be recorded.
- Attribution and licences for third-party test files; the repo's own licence.
- A script that clones and builds upstream development revisions (with ASan/UBSan builds).
- Minimizing the suite.
- APNG frames, then colour management, then encoders.
