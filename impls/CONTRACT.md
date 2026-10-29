# Decoder wrapper contract

A decoder column is any program the harness can run as

    <cmd...> <input.png> <output.pam>

(`{input}` and `{output}` in `impls.toml` are substituted). The harness
runs it in a scratch directory under an address-space limit, an output-size
limit and a wall-clock timeout, and records exit status, signal, wall time,
stdout and stderr.

## Output

- **Exit 0 and a PAM at `<output.pam>`** means the decoder produced an image.
  PAM (`P7`) with DEPTH 1..4 (G, GA, RGB, RGBA), MAXVAL 255 or 65535
  (other maxvals are accepted and scaled), 16-bit samples big-endian.
  Palette and sub-byte depths must be expanded by the wrapper or decoder;
  that expansion is spec-exact so nothing is lost. Nothing else may be
  applied: no gamma, no background compositing, no 16->8.
- **Exit 127** means the decoder is not installed (the shell's status for a command it cannot find;
  shims use it when their decoder library fails to load). The harness then skips the column with a
  notice instead of judging it.
- **Exit 71 and `API <message>`** means the library returned without error, but what it
  returned is not an image: too few or too many rows, a size other than the declared one,
  samples that are not numbers or exceed the depth, a layout the shim cannot read. The
  library accepted the file and produced no usable result, so the verdict is `api`, a
  must-violation under both lenses, never a rejection. The message says what was missing.
  For a lazy row iterator, finish consuming it first: a later decoder error takes precedence.
- **Exit 70 and `SHIM <message>`** means the shim itself failed: it could not open or write
  its output, or one of its own invariants broke. The verdict is `shim`, which is not scored;
  the run lists it under problems for someone to fix. Dying by the harness's output-size
  limit (`SIGXFSZ`) counts as this too. Keep library calls, output checks and shim work in
  separate error paths so none can pass for another.
- **Any other non-zero exit** means reject, unless the process crashed or exhausted memory.
  Preserve exception causes, including allocation failures hidden in a wrapper exception.
  The harness takes the reason from the last
  `ERR <message>` line on stdout, else the last non-empty stderr line.
- A decoder that is a validator only (no pixels) sets `pixels = false` and is
  judged on exit status alone.
- **Bounded buffers.** The shim holds a decoder row, an Adam7 merge row or a band of rows
  of bounded size, never another copy of the image. It writes rows into the output as the
  decoder produces them (`impls/pam_rows.h` does this for C shims). A decoder whose API
  only hands over whole images allocates them itself, and that is its own result.
- **An error after the rows is an error.** When the decoder reports an error after the last
  row (a bad trailer, a checksum, an unknown critical chunk after IDAT), the shim exits
  non-zero like for any other error. Whether the rows already decoded are usable is the
  application's decision, which the shim does not make. The output may then hold the rows
  written before the error; the harness ignores it when the exit status is non-zero.
  Take the step after the rows that the library's own whole-image call takes, and no other:
  `png_read_png` calls `png_read_end`, so the libpng shim does; libspng's `spng_decode_image`
  and the png crate's `next_frame` read nothing after the rows, so their shims don't either.

Each column records its diagnostic channel in `impls.toml`, including when its API has
no recoverable-warning channel. Connect exposed warning callbacks and relay library
diagnostics; do not manufacture warnings from adapter housekeeping or a preliminary
API call that is unnecessary to decode the image. Raw output is capped for display, but
diagnostic classification scans all of it with bounded memory.

## Optional stdout lines (one per line, before the final verdict)

- `INFO <key>=<value> ...` free-form facts the decoder reports (IHDR fields,
  chunk presence). Stored verbatim.
- `WARN <text>` a non-fatal complaint. Any WARN/ERR line, or any nonempty stderr output,
  marks an accept as carrying a diagnostic; it does not change its severity.
- `OK <w> <h> <channels> <depth>` / `ERR <message>` / `API <message>` / `SHIM <message>` final line.

## Verdicts the harness assigns

`accept` (with an optional warning), `reject`, `api`, `crash` (died by signal), `timeout`,
`oom` (exit or abort with an allocation-failure message), and `shim`, which is not scored.

## Animation (not yet used)

A shim returns one image, decoded the way a still PNG is. For an APNG that should be the
default image; a library whose plain path yields an animation frame instead shows that in
its results. Testing animation will generalize the output to a sequence of frames (one PAM
per frame plus each frame's delay), with single-image decoding as the one-frame case.
