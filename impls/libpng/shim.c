/* libpng shim: decode one PNG with libpng's default policies and emit PAM.
 *
 * Policy (deliberate, measured, not hidden):
 *   - transformations: png_set_expand only (palette->RGB, tRNS->alpha,
 *     gray<8 -> 8-bit, all spec-exact). No gamma, no background, no 16->8.
 *   - CRC handling, unknown-chunk handling, user limits: libpng defaults.
 *   - png_read_end IS called. An error raised after png_read_image completed
 *     (trailer problems) is reported as "WARN after image:" and the image is
 *     still written, so the harness can see "image complete, trailer bad".
 *
 * stdout protocol: INFO ... / WARN ... lines, then "OK w h channels depth"
 * or "ERR message". Exit 0 only on OK.
 */
#include <png.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* Statics, not locals: they are written after setjmp and read after longjmp. */
static png_structp png;
static png_infop info;
static volatile int image_done = 0;
static png_uint_32 w, h;
static int channels, odepth;
static size_t rowbytes;
static png_bytep buf;
static png_bytep *rows;

static void warn_fn(png_structp p, png_const_charp msg)
{
	(void)p;
	printf("WARN %s\n", msg);
}

static void err_fn(png_structp p, png_const_charp msg)
{
	if (image_done)
		printf("WARN after image: %s\n", msg);
	else
		printf("ERR %s\n", msg);
	fflush(stdout);
	png_longjmp(p, 1);
}

static void report_chunks(void)
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
	if (!in) {
		printf("ERR cannot open input\n");
		return 1;
	}
	png = png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, err_fn, warn_fn);
	info = png ? png_create_info_struct(png) : NULL;
	if (!png || !info) {
		printf("ERR png_create_*_struct failed\n");
		return 1;
	}
	if (setjmp(png_jmpbuf(png))) {
		if (!image_done)
			return 1;
		goto write_out;
	}
	png_init_io(png, in);
	png_read_info(png, info);
	{
		int depth, ctype, interlace, comp, filt;
		png_get_IHDR(png, info, &w, &h, &depth, &ctype, &interlace, &comp, &filt);
		printf("INFO ihdr width=%u height=%u bit_depth=%d color_type=%d interlace=%d\n",
			(unsigned)w, (unsigned)h, depth, ctype, interlace);
	}
	report_chunks();

	png_set_expand(png);
	png_set_interlace_handling(png);
	png_read_update_info(png, info);
	channels = png_get_channels(png, info);
	odepth = png_get_bit_depth(png, info);
	rowbytes = png_get_rowbytes(png, info);
	if (odepth != 8 && odepth != 16) {
		printf("ERR unexpected output bit depth %d\n", odepth);
		return 1;
	}
	if (h != 0 && rowbytes > SIZE_MAX / h) {
		printf("ERR image too large for address space\n");
		return 1;
	}
	buf = malloc(rowbytes * h);
	rows = malloc(sizeof(png_bytep) * h);
	if (!buf || !rows) {
		printf("ERR out of memory (%zu bytes)\n", rowbytes * h);
		return 1;
	}
	for (png_uint_32 i = 0; i < h; i++)
		rows[i] = buf + (size_t)i * rowbytes;
	png_read_image(png, rows);
	image_done = 1;
	png_read_end(png, info);

write_out:
	{
		FILE *out = fopen(argv[2], "wb");
		if (!out) {
			printf("ERR cannot open output\n");
			return 1;
		}
		const char *tt = channels == 1 ? "GRAYSCALE" : channels == 2 ? "GRAYSCALE_ALPHA"
			: channels == 3 ? "RGB" : "RGB_ALPHA";
		fprintf(out, "P7\nWIDTH %u\nHEIGHT %u\nDEPTH %d\nMAXVAL %d\nTUPLTYPE %s\nENDHDR\n",
			(unsigned)w, (unsigned)h, channels, odepth == 16 ? 65535 : 255, tt);
		if (fwrite(buf, 1, rowbytes * h, out) != rowbytes * h || fclose(out) != 0) {
			printf("ERR short write\n");
			return 1;
		}
	}
	printf("OK %u %u %d %d\n", (unsigned)w, (unsigned)h, channels, odepth);
	return 0;
}
