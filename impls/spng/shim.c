/* libspng shim: decode one PNG with libspng defaults and emit PAM.
 *
 * Policy: SPNG_FMT_RGBA8 for files up to 8 bits, SPNG_FMT_RGBA16 for 16-bit,
 * with SPNG_DECODE_TRNS so tRNS becomes alpha (spng does not apply it unless
 * asked). No gamma flag. CRC actions and limits are libspng's defaults.
 */
#include <spng.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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
	if (!in) {
		printf("ERR cannot open input\n");
		return 1;
	}
	spng_ctx *ctx = spng_ctx_new(0);
	int r = spng_set_png_file(ctx, in);
	if (r) {
		printf("ERR %s\n", spng_strerror(r));
		return 1;
	}
	struct spng_ihdr ihdr;
	r = spng_get_ihdr(ctx, &ihdr);
	if (r) {
		printf("ERR %s\n", spng_strerror(r));
		return 1;
	}
	printf("INFO ihdr width=%u height=%u bit_depth=%u color_type=%u interlace=%u\n",
		ihdr.width, ihdr.height, ihdr.bit_depth, ihdr.color_type, ihdr.interlace_method);
	int fmt = ihdr.bit_depth == 16 ? SPNG_FMT_RGBA16 : SPNG_FMT_RGBA8;
	size_t len;
	r = spng_decoded_image_size(ctx, fmt, &len);
	if (r) {
		printf("ERR %s\n", spng_strerror(r));
		return 1;
	}
	unsigned char *buf = malloc(len);
	if (!buf) {
		printf("ERR out of memory (%zu bytes)\n", len);
		return 1;
	}
	r = spng_decode_image(ctx, buf, len, fmt, SPNG_DECODE_TRNS);
	if (r) {
		printf("ERR %s\n", spng_strerror(r));
		return 1;
	}
	if (fmt == SPNG_FMT_RGBA16) { /* host-endian u16 -> big-endian */
		for (size_t i = 0; i + 1 < len; i += 2) {
			unsigned short v;
			memcpy(&v, buf + i, 2);
			buf[i] = (unsigned char)(v >> 8);
			buf[i + 1] = (unsigned char)(v & 0xff);
		}
	}
	FILE *o = fopen(argv[2], "wb");
	if (!o) {
		printf("ERR cannot open output\n");
		return 1;
	}
	fprintf(o, "P7\nWIDTH %u\nHEIGHT %u\nDEPTH 4\nMAXVAL %d\nTUPLTYPE RGB_ALPHA\nENDHDR\n",
		ihdr.width, ihdr.height, fmt == SPNG_FMT_RGBA16 ? 65535 : 255);
	if (fwrite(buf, 1, len, o) != len || fclose(o) != 0) {
		printf("ERR short write\n");
		return 1;
	}
	printf("OK %u %u 4 %d\n", ihdr.width, ihdr.height, fmt == SPNG_FMT_RGBA16 ? 16 : 8);
	return 0;
}
