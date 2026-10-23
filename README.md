# difftest-png

A small suite of PNG files, each built to test one rule of the PNG specification or one
behaviour on which some decoder deviates, and a script that runs it against every PNG decoder
on your machine. The output answers "which PNG library should I use?" and "how reliable is my
software?": a JSON report and a single self-contained HTML page.

The published run is at <https://wizardmaxx.ing/difftest-png/>.

## Human disclaimer

The results likely contain errors, or decisions which can be argued with. Regardless, I did take greater-than-usual care to make this meaningful. The code is probably vibeslop, and I haven't read a single line. But, the decisions here came from several rounds of [Matt Pocock's grill-me Skill](https://github.com/mattpocock/skills) and at least four separate essays I wrote while trying to keep Claude Opus 5.5 on track, alongside several rounds of adversarial checking from GPT-6 Astra and from Claude Fable 5.1. I had to "push back" quite a lot to get past the AI's boneheaded decisions.

My conclusion: [Parsing PNGs is a Minefield](https://seriot.ch/security/parsing_json.html). A modern JSON comparison is coming Soon™, don't worry :)

## Run it

	uv run run.py                 # every implementation found here
	uv run run.py libpng wuffs    # only these (the reference decoder always runs)

Shims with a build step are compiled first if their toolchain is present; any implementation
whose program is missing is skipped with a notice. Results go to `out/report.json` and
`out/report.html`. Options: `--no-memcheck` skips the valgrind pass, `--no-build` skips
compiling, `--jobs N`, `--out DIR`, `--head DIR` (see below). Every run is fresh; while
iterating, `--reuse` takes stored results from earlier runs for every implementation except the
reference decoder, and `--rerun IMPL ...` runs the named ones afresh (do that after rebuilding
or upgrading one). A program that is not installed (or a shim whose library is missing, exit
127) is skipped with a notice rather than judged.

Linux only (the runner uses `resource` limits). Needs Python via `uv`; each implementation
needs its own toolchain:

| implementations | needs |
|---|---|
| reference-ed2, reference-ed3, pillow, pypng | nothing beyond `uv` |
| opencv | `uv sync --extra opencv` |
| libpng, libspng | their headers (`libpng`, `libspng`) and a C compiler |
| stb_image, lodepng, wuffs | a C/C++ compiler (sources are in `impls/`) |
| rust-png, zune-png | cargo |
| go-png | Go |
| java-imageio | a JDK |
| imagesharp | .NET SDK 9 |
| fast-png, pngjs, upng | node and npm |
| chunky_png | ruby and `gem install --user-install chunky_png -v 1.4.0` |
| ffmpeg, imagemagick, graphicsmagick, pngtopam, pngcheck | the installed program |
| memcheck pass | valgrind |

## What a test is

`decode/manifest.tsv` has one row per file in `decode/`:

- `ed2`, `ed3`: the outcomes the test allows under the second (ISO/IEC 15948:2003) and third
  (W3C 2025) editions, each with a severity: `pixels=pass,reject=should` means decoding to
  the expected pixels passes and rejecting the file is a should-violation. Anything not listed,
  and any crash, hang, out-of-memory or memory error, is a must-violation.
- `hash2`, `hash3`: digest of the expected pixels (RGBA, 16 bits per sample) per edition.
  Two kinds of file also accept other pixels, which `run.py` derives with the reference decoder
  at run time and the report marks: an ancillary chunk with a bad CRC used as stored (✓ᶜ),
  and several tRNS chunks with the last one kept (✓ᵗ).
- `clause`: the clause of `spec/clauses.tsv` the test is about; `reaches`: every clause the
  reference decoder reaches on the file. `run.py` fails if a file stops reaching one of them,
  so an optimizer or a hand edit cannot quietly turn a test into a different test.
- The filename prefix summarises the third-edition outcomes: `y_` must decode, `n_` must be
  rejected, `r_` should be recovered, `i_` is the implementation's choice.

Expectations come from the reference decoder (`refpng.py`, with its own inflater in
`inflate.py`), which checks every clause of `spec/clauses.tsv` and names it in each message.
After editing a file, `uv run expect.py` refreshes the hashes and traces (it refuses to drop a
declared clause without `--retrace`). Expectations come only from the faults the reference reports,
so `expect.py` also refuses any file on which the reference reaches a fault clause without
reporting it.

Pixels are compared with no colour management. Colour kept under fully transparent pixels
(preserved, zeroed, or altered) and how 8-bit-only decoders reduce 16-bit images are recorded
as properties of the implementation, not failures. A test where the two editions require
different results passes if either is met; the summary says which edition each implementation
is consistent with, and "mixed" when it follows each edition on different tests.

## Upstream development revisions

Every implementation in `impls.toml` is a column, and a project can have several: the release
and HEAD of the same library are separate implementations. HEAD columns refer to `{head}`, a
directory of upstream builds (default `~/.cache/difftest-png/head`, or `--head`); they are
skipped when it does not hold them. A script to clone and build them is not written yet.

## Layout

- `DECISIONS.md` every design decision behind the suite and the report, and why
- `run.py` runs the suite and writes the report; `execute.py` runs one implementation on one
  file; `pam.py` reads their output; `report.py` renders the HTML
- `refpng.py`, `pngfields.py`, `pngdiagnostics.py`, `inflate.py` the reference decoder;
  `spec/clauses.tsv` the clauses it checks; `test_refpng.py` its regression tests
- `impls.toml`, `impls/` the implementations and the shim each one runs through
  (`impls/CONTRACT.md`)
- `expect.py` derives expectations; `pick.py` chooses the suite from a candidate pool (every
  conformance test, and every file any implementation fails); `minimize.py` shrinks each file
  while every implementation's outcome stays the same (optional)
- `decode/` the test files; `encode/` will hold encoder tests
