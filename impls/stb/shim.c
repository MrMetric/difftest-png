/* stb_image shim: decode one PNG with stb_image's defaults and emit PAM.
 *
 * Policy: req_comp = 0 (native channel count as stb reports it; stb itself
 * always expands palette to RGB/RGBA and low bit depths to 8). 16-bit images
 * go through stbi_load_16 and are byte-swapped to big-endian for PAM.
 * stb ignores CRCs; CgBI conversion and unpremultiplication default off,
 * not configured away.
 */
#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#include "stb_image.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef STB_VER
#define STB_VER "unknown"
#endif

static int write_pam(const char *path, int w, int h, int n, int is16, const void *data)
{
	FILE *out = fopen(path, "wb");
	if (!out) {
		printf("SHIM cannot open output\n");
		return 0;
	}
	const char *tt = n == 1 ? "GRAYSCALE" : n == 2 ? "GRAYSCALE_ALPHA" : n == 3 ? "RGB" : "RGB_ALPHA";
	fprintf(out, "P7\nWIDTH %d\nHEIGHT %d\nDEPTH %d\nMAXVAL %d\nTUPLTYPE %s\nENDHDR\n",
		w, h, n, is16 ? 65535 : 255, tt);
	size_t samples = (size_t)w * (size_t)n;
	int ok = 1;
	if (is16) {
		const unsigned short *s = data;
		unsigned char *be = malloc(samples * 2);
		if (!be) {
			printf("SHIM out of memory for one row\n");
			fclose(out);
			return 0;
		}
		for (int y = 0; y < h && ok; y++) {
			for (size_t x = 0; x < samples; x++) {
				unsigned short v = s[(size_t)y * samples + x];
				be[2 * x] = (unsigned char)(v >> 8);
				be[2 * x + 1] = (unsigned char)v;
			}
			ok = fwrite(be, 2, samples, out) == samples;
		}
		free(be);
	} else {
		const unsigned char *s = data;
		for (int y = 0; y < h && ok; y++)
			ok = fwrite(s + (size_t)y * samples, 1, samples, out) == samples;
	}
	if (fclose(out) != 0) ok = 0;
	if (!ok) {
		printf("SHIM short write\n");
		return 0;
	}
	return 1;
}

int main(int argc, char **argv)
{
	if (argc == 2 && strcmp(argv[1], "--version") == 0) {
		printf("stb_image %s\n", STB_VER);
		return 0;
	}
	if (argc != 3) {
		fprintf(stderr, "usage: shim in.png out.pam\n");
		return 2;
	}
	int w, h, n;
	if (!stbi_info(argv[1], &w, &h, &n)) {
		printf("ERR %s\n", stbi_failure_reason());
		return 1;
	}
	int is16 = stbi_is_16_bit(argv[1]);
	printf("INFO stbi_info width=%d height=%d channels=%d is16=%d\n", w, h, n, is16);
	void *data = is16 ? (void *)stbi_load_16(argv[1], &w, &h, &n, 0)
			  : (void *)stbi_load(argv[1], &w, &h, &n, 0);
	if (!data) {
		printf("ERR %s\n", stbi_failure_reason());
		return 1;
	}
	if (w <= 0 || h <= 0 || n < 1 || n > 4) {
		printf("API invalid image dimensions or channel count: %dx%d, %d channels\n", w, h, n);
		stbi_image_free(data);
		return 71;
	}
	int wrote = write_pam(argv[2], w, h, n, is16, data);
	stbi_image_free(data);
	if (!wrote) return 70;
	printf("OK %d %d %d %d\n", w, h, n, is16 ? 16 : 8);
	return 0;
}
