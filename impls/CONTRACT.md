# Decoder wrapper contract

A decoder column is any program the harness can run as

    <cmd...> <input.png> <output.pam>

(`{input}` and `{output}` in `impls.toml` are substituted). The harness
runs it in a scratch directory under an address-space limit, an output-size
limit and a wall-clock timeout, and records exit status, signal, wall time,
peak RSS, stdout and stderr.

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
- **Any other non-zero exit** means reject. The harness takes the reason from the last
  `ERR <message>` line on stdout, else the last non-empty stderr line.
- A decoder that is a validator only (no pixels) sets `pixels = false` and is
  judged on exit status alone.
- **One row at a time.** The shim holds at most one row of pixels and writes rows into the
  output as the decoder produces them (`impls/pam_rows.h` does this for C shims). It never
  allocates the image itself, so it has no memory need the decoder lacks. A decoder whose API
  only hands over whole images allocates them itself, and that is its own result.
- **An error after the rows is an error.** When the decoder reports an error after the last
  row (a bad trailer, a checksum, an unknown critical chunk after IDAT), the shim exits
  non-zero like for any other error. Whether the rows already decoded are usable is the
  application's decision, which the shim does not make. The output may then hold the rows
  written before the error; the harness ignores it when the exit status is non-zero.

## Optional stdout lines (one per line, before the final verdict)

- `INFO <key>=<value> ...` free-form facts the decoder reports (IHDR fields,
  chunk presence). Stored verbatim.
- `WARN <text>` a non-fatal complaint. Any WARN line, or any stderr output,
  turns an accept into `warn`.
- `OK <w> <h> <channels> <depth>` / `ERR <message>` final line.

## Verdicts the harness assigns

`accept`, `warn`, `reject`, `crash` (died by signal), `timeout`, `oom`
(exit or abort with an allocation-failure message).

## Animation (not yet used)

A shim returns one image: the default image of an APNG. Testing animation will generalise the
output to a sequence of frames (one PAM per frame plus each frame's delay), with single-image
decoding as the one-frame case.
