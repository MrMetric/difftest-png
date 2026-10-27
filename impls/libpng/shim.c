/* libpng shim: decode one PNG with libpng's default policies and emit PAM.
 *
 * Policy (deliberate, measured, not hidden):
 *   - transformations: png_set_expand only (palette->RGB, tRNS->alpha,
 *     gray<8 -> 8-bit, all spec-exact). No gamma, no background, no 16->8.
 *   - CRC handling, unknown-chunk handling, user limits: libpng defaults.
 *   - Rows are read one at a time with png_read_row and written out as they
 *     arrive, so the shim holds one row and never the image: it has no memory
 *     need libpng itself lacks. Interlace handling stays off; the shim reads
 *     each pass's rows and places their pixels itself, the way libpng-manual.txt
 *     describes with the PNG_PASS_* macros.
 *   - png_read_end is called. An error libpng raises at any point, before or
 *     after the rows, is reported as ERR and the shim exits 1. Whether an image
 *     whose trailer is bad is still usable is the caller's decision, not the
 *     shim's.
 *
 * stdout protocol: INFO ... / WARN ... lines, then "OK w h channels depth"
 * or "ERR message". Exit 0 only on OK.
 */
#include "../pam_rows.h"
#include <png.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void warn_fn(png_structp p, png_const_charp msg)
{
	(void)p;
	printf("WARN %s\n", msg);
}

static void err_fn(png_structp p, png_const_charp msg)
{
	printf("ERR %s\n", msg);
	fflush(stdout);
	png_longjmp(p, 1);
}

static int fail(const char *msg)
{
	printf("ERR %s\n", msg);
	return 1;
}

static void report_chunks(png_structp png, png_infop info)
{
	static const struct { png_uint_32 flag; const char *name; } known[] = {
		{PNG_INFO_PLTE, "PLTE"}, {PNG_INFO_tRNS, "tRNS"}, {PNG_INFO_gAMA, "gAMA"},
		{PNG_INFO_cHRM, "cHRM"}, {PNG_INFO_sRGB, "sRGB"}, {PNG_INFO_iCCP, "iCCP"},
		{PNG_INFO_sBIT, "sBIT"}, {PNG_INFO_bKGD, "bKGD"}, {PNG_INFO_hIST, "hIST"},
		{PNG_INFO_pHYs, "pHYs"}, {PNG_INFO_oFFs, "oFFs"}, {PNG_INFO_tIME, "tIME"},
		{PNG_INFO_pCAL, "pCAL"}, {PNG_INFO_sCAL, "sCAL"}, {PNG_INFO_sPLT, "sPLT"},
#ifdef PNG_INFO_eXIf
		{PNG_INFO_eXIf, "eXIf"},
#endif
	};
	printf("INFO chunks=");
	int first = 1;
	for (size_t i = 0; i < sizeof known / sizeof known[0]; i++) {
		if (png_get_valid(png, info, known[i].flag)) {
			printf("%s%s", first ? "" : ",", known[i].name);
			first = 0;
		}
	}
	png_textp text;
	int ntext = 0;
	png_get_text(png, info, &text, &ntext);
	printf(" text=%d\n", ntext);
}

int main(int argc, char **argv)
{
	if (argc == 2 && strcmp(argv[1], "--version") == 0) {
		printf("libpng %s (built against %s)\n", png_get_libpng_ver(NULL), PNG_LIBPNG_VER_STRING);
		return 0;
	}
	if (argc != 3) {
		fprintf(stderr, "usage: shim in.png out.pam\n");
		return 2;
	}
	FILE *in = fopen(argv[1], "rb");
	if (!in)
		return fail("cannot open input");
	png_structp png = png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, err_fn, warn_fn);
	png_infop info = png ? png_create_info_struct(png) : NULL;
	if (!png || !info)
		return fail("png_create_*_struct failed");
	if (setjmp(png_jmpbuf(png)))
		return 1; /* err_fn has reported libpng's error */
	png_init_io(png, in);
	png_read_info(png, info);
	png_uint_32 w, h;
	int depth, ctype, interlace, comp, filt;
	png_get_IHDR(png, info, &w, &h, &depth, &ctype, &interlace, &comp, &filt);
	printf("INFO ihdr width=%u height=%u bit_depth=%d color_type=%d interlace=%d\n",
		(unsigned)w, (unsigned)h, depth, ctype, interlace);
	report_chunks(png, info);

	png_set_expand(png);
	png_read_update_info(png, info);
	int channels = png_get_channels(png, info), odepth = png_get_bit_depth(png, info);
	size_t rowbytes = png_get_rowbytes(png, info);
	if (odepth != 8 && odepth != 16) {
		printf("ERR unexpected output bit depth %d\n", odepth);
		return 1;
	}
	struct pam_out out;
	if (pam_open(&out, argv[2], w, h, channels, odepth == 16 ? 65535 : 255))
		return fail("cannot open output");
	png_bytep row = malloc(rowbytes), scratch = malloc(rowbytes);
	if (!row || !scratch)
		return fail("out of memory for one row");
	if (interlace == PNG_INTERLACE_ADAM7) {
		for (int pass = 0; pass < PNG_INTERLACE_ADAM7_PASSES; pass++) {
			png_uint_32 cols = PNG_PASS_COLS(w, pass), rows = PNG_PASS_ROWS(h, pass);
			if (cols == 0 || rows == 0)
				continue; /* an empty pass has no rows in the datastream */
			for (png_uint_32 y = 0; y < rows; y++) {
				png_read_row(png, row, NULL);
				if (pam_pass_row(&out, pass, y, row, cols, scratch))
					return fail("cannot write output");
			}
		}
	} else {
		for (png_uint_32 y = 0; y < h; y++) {
			png_read_row(png, row, NULL);
			if (pam_row(&out, y, row))
				return fail("cannot write output");
		}
	}
	png_read_end(png, info);
	if (pam_close(&out))
		return fail("output incomplete");
	printf("OK %u %u %d %d\n", (unsigned)w, (unsigned)h, channels, odepth);
	return 0;
}
