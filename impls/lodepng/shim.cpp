/* lodepng shim: decode one PNG with lodepng defaults and emit PAM.
 *
 * Policy: lodepng converts to the requested raw format; we request RGBA at
 * the file's sample depth (8 for anything up to 8 bits, 16 for 16), which
 * expands palette, tRNS and low bit depths exactly. CRC and Adler-32 checks
 * stay at lodepng's defaults (both on). 16-bit output is big-endian already.
 */
#include "lodepng.h"
#include <cstdio>
#include <cstring>
#include <vector>

int main(int argc, char **argv)
{
	if (argc == 2 && !strcmp(argv[1], "--version")) {
		printf("lodepng %s\n", LODEPNG_VERSION_STRING);
		return 0;
	}
	if (argc != 3) {
		fprintf(stderr, "usage: shim in.png out.pam\n");
		return 2;
	}
	std::vector<unsigned char> file;
	unsigned err = lodepng::load_file(file, argv[1]);
	if (err) {
		printf("ERR %s\n", lodepng_error_text(err));
		return 1;
	}
	lodepng::State state;
	unsigned w = 0, h = 0;
	err = lodepng_inspect(&w, &h, &state, file.data(), file.size());
	if (err) {
		printf("ERR %s\n", lodepng_error_text(err));
		return 1;
	}
	const LodePNGColorMode &c = state.info_png.color;
	printf("INFO ihdr width=%u height=%u bit_depth=%u color_type=%d interlace=%u\n",
		w, h, c.bitdepth, (int)c.colortype, state.info_png.interlace_method);
	unsigned outdepth = c.bitdepth == 16 ? 16 : 8;
	state.info_raw.colortype = LCT_RGBA;
	state.info_raw.bitdepth = outdepth;
	std::vector<unsigned char> out;
	err = lodepng::decode(out, w, h, state, file);
	if (err) {
		printf("ERR %s\n", lodepng_error_text(err));
		return 1;
	}
	FILE *o = fopen(argv[2], "wb");
	if (!o) {
		printf("ERR cannot open output\n");
		return 1;
	}
	fprintf(o, "P7\nWIDTH %u\nHEIGHT %u\nDEPTH 4\nMAXVAL %d\nTUPLTYPE RGB_ALPHA\nENDHDR\n", w, h, outdepth == 16 ? 65535 : 255);
	if (fwrite(out.data(), 1, out.size(), o) != out.size() || fclose(o) != 0) {
		printf("ERR short write\n");
		return 1;
	}
	printf("OK %u %u 4 %u\n", w, h, outdepth);
	return 0;
}
