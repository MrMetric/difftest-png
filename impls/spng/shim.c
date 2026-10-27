/* libspng shim: decode one PNG with libspng defaults and emit PAM.
 *
 * Policy: SPNG_FMT_RGBA8 for files up to 8 bits, SPNG_FMT_RGBA16 for 16-bit,
 * with SPNG_DECODE_TRNS so tRNS becomes alpha (spng does not apply it unless
 * asked). No gamma flag. CRC actions and limits are libspng's defaults.
 * Rows are decoded one at a time (SPNG_DECODE_PROGRESSIVE, spng_decode_row)
 * and written out as they arrive, so the shim holds one row and never the
 * image. libspng puts an interlace pass's pixels at their columns inside a
 * full-width row buffer; the shim packs them and places them in the output
 * row. Any error libspng reports is ERR and exit 1.
 */
#include "../pam_rows.h"
#include <spng.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fail(const char *msg)
{
	printf("ERR %s\n", msg);
	return 1;
}

int main(int argc, char **argv)
{
	if (argc == 2 && !strcmp(argv[1], "--version")) {
		printf("libspng %s\n", spng_version_string());
		return 0;
	}
	if (argc != 3) {
		fprintf(stderr, "usage: shim in.png out.pam\n");
		return 2;
	}
	FILE *in = fopen(argv[1], "rb");
	if (!in)
		return fail("cannot open input");
	spng_ctx *ctx = spng_ctx_new(0);
	int r = spng_set_png_file(ctx, in);
	if (r)
		return fail(spng_strerror(r));
	struct spng_ihdr ihdr;
	r = spng_get_ihdr(ctx, &ihdr);
	if (r)
		return fail(spng_strerror(r));
	printf("INFO ihdr width=%u height=%u bit_depth=%u color_type=%u interlace=%u\n",
		ihdr.width, ihdr.height, ihdr.bit_depth, ihdr.color_type, ihdr.interlace_method);
	int fmt = ihdr.bit_depth == 16 ? SPNG_FMT_RGBA16 : SPNG_FMT_RGBA8;
	size_t bpp = fmt == SPNG_FMT_RGBA16 ? 8 : 4, rowlen = (size_t)ihdr.width * bpp;
	r = spng_decode_image(ctx, NULL, 0, fmt, SPNG_DECODE_TRNS | SPNG_DECODE_PROGRESSIVE);
	if (r)
		return fail(spng_strerror(r));
	unsigned char *row = malloc(rowlen), *scratch = malloc(rowlen);
	if (!row || !scratch)
		return fail("out of memory for one row");
	struct pam_out out;
	if (pam_open(&out, argv[2], ihdr.width, ihdr.height, 4, fmt == SPNG_FMT_RGBA16 ? 65535 : 255))
		return fail("cannot open output");
	for (;;) {
		struct spng_row_info ri;
		r = spng_get_row_info(ctx, &ri);
		if (r == SPNG_EOI)
			break;
		if (r)
			return fail(spng_strerror(r));
		memset(row, 0, rowlen);
		r = spng_decode_row(ctx, row, rowlen); /* SPNG_EOI: this row decoded, and it was the last */
		if (r && r != SPNG_EOI)
			return fail(spng_strerror(r));
		unsigned n = ihdr.width;
		if (ihdr.interlace_method) {
			const unsigned *a = PAM_ADAM7[ri.pass];
			n = pam_pass_cols(ihdr.width, ri.pass);
			for (unsigned i = 0; i < n; i++) /* pack: pixel i sits at column a[0] + i*a[2], never left of slot i */
				memmove(row + (size_t)i * bpp, row + ((size_t)a[0] + (size_t)i * a[2]) * bpp, bpp);
		}
		if (fmt == SPNG_FMT_RGBA16) { /* host-endian u16 -> big-endian */
			for (size_t i = 0; i + 1 < (size_t)n * bpp; i += 2) {
				unsigned short v;
				memcpy(&v, row + i, 2);
				row[i] = (unsigned char)(v >> 8);
				row[i + 1] = (unsigned char)(v & 0xff);
			}
		}
		int w = ihdr.interlace_method
			? pam_pass_row(&out, ri.pass, (ri.row_num - PAM_ADAM7[ri.pass][1]) / PAM_ADAM7[ri.pass][3], row, n, scratch)
			: pam_row(&out, ri.row_num, row);
		if (w)
			return fail("cannot write output");
		if (r == SPNG_EOI)
			break;
	}
	if (pam_close(&out))
		return fail("output incomplete");
	printf("OK %u %u 4 %d\n", ihdr.width, ihdr.height, fmt == SPNG_FMT_RGBA16 ? 16 : 8);
	return 0;
}
