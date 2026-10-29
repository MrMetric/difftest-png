/* wuffs shim: decode one PNG through wuffs_aux::DecodeImage and emit PAM.
 *
 * Policy: the aux helper's defaults (checksums verified, default dimension
 * and metadata limits), except the destination pixel format, which is chosen
 * from the decoder's natural format so no sample is rescaled: 16-bit sources
 * go to BGRA_NONPREMUL_4X16LE (the only 16-bit destination wuffs's PNG swizzler
 * offers; reordered to RGBA here), everything else to RGBA_NONPREMUL. Palette,
 * tRNS and sub-byte depths are expanded by wuffs's own swizzler.
 */
#define WUFFS_IMPLEMENTATION
#define WUFFS_CONFIG__STATIC_FUNCTIONS
#define WUFFS_CONFIG__MODULES
#define WUFFS_CONFIG__MODULE__AUX__BASE
#define WUFFS_CONFIG__MODULE__AUX__IMAGE
#define WUFFS_CONFIG__MODULE__BASE
#define WUFFS_CONFIG__MODULE__ADLER32
#define WUFFS_CONFIG__MODULE__CRC32
#define WUFFS_CONFIG__MODULE__DEFLATE
#define WUFFS_CONFIG__MODULE__PNG
#define WUFFS_CONFIG__MODULE__ZLIB
#include "wuffs-v0.4.c"

#include <cstdio>
#include <cstring>
#include <string>

static bool is16(uint32_t repr)
{
	return repr == WUFFS_BASE__PIXEL_FORMAT__Y_16BE || repr == WUFFS_BASE__PIXEL_FORMAT__Y_16LE
		|| repr == WUFFS_BASE__PIXEL_FORMAT__BGRA_NONPREMUL_4X16LE || repr == WUFFS_BASE__PIXEL_FORMAT__BGRA_PREMUL_4X16LE
		|| repr == WUFFS_BASE__PIXEL_FORMAT__RGBA_NONPREMUL_4X16LE || repr == WUFFS_BASE__PIXEL_FORMAT__RGBA_PREMUL_4X16LE;
}

class Callbacks : public wuffs_aux::DecodeImageCallbacks {
public:
	uint32_t natural = 0;
	wuffs_base__pixel_format SelectPixfmt(const wuffs_base__image_config &ic) override
	{
		natural = ic.pixcfg.pixel_format().repr;
		return wuffs_base__make_pixel_format(is16(natural) ? WUFFS_BASE__PIXEL_FORMAT__BGRA_NONPREMUL_4X16LE
			: WUFFS_BASE__PIXEL_FORMAT__RGBA_NONPREMUL);
	}
};

int main(int argc, char **argv)
{
	if (argc == 2 && !strcmp(argv[1], "--version")) {
		printf("wuffs %s\n", WUFFS_VERSION_STRING);
		return 0;
	}
	if (argc != 3) {
		fprintf(stderr, "usage: shim in.png out.pam\n");
		return 2;
	}
	FILE *f = fopen(argv[1], "rb");
	if (!f) {
		printf("ERR cannot open input\n");
		return 1;
	}
	Callbacks cb;
	wuffs_aux::sync_io::FileInput input(f);
	wuffs_aux::DecodeImageResult res = wuffs_aux::DecodeImage(cb, input);
	printf("INFO natural_pixfmt=0x%08X\n", cb.natural);
	if (!res.error_message.empty()) {
		printf("ERR %s\n", res.error_message.c_str());
		return 1;
	}
	wuffs_base__pixel_buffer &pb = res.pixbuf;
	uint32_t w = pb.pixcfg.width(), h = pb.pixcfg.height();
	bool wide = pb.pixcfg.pixel_format().repr == WUFFS_BASE__PIXEL_FORMAT__BGRA_NONPREMUL_4X16LE;
	wuffs_base__table_u8 t = pb.plane(0);
	FILE *o = fopen(argv[2], "wb");
	if (!o) {
		printf("SHIM cannot open output\n");
		return 70;
	}
	fprintf(o, "P7\nWIDTH %u\nHEIGHT %u\nDEPTH 4\nMAXVAL %d\nTUPLTYPE RGB_ALPHA\nENDHDR\n", w, h, wide ? 65535 : 255);
	std::string row;
	for (uint32_t y = 0; y < h; y++) {
		const uint8_t *src = t.ptr + (size_t)y * t.stride;
		if (wide) { /* B,G,R,A little-endian u16 -> R,G,B,A big-endian */
			static const int order[4] = {2, 1, 0, 3};
			row.resize((size_t)w * 8);
			for (uint32_t x = 0; x < w; x++) {
				for (int c = 0; c < 4; c++) {
					row[8 * x + 2 * c] = (char)src[8 * x + 2 * order[c] + 1];
					row[8 * x + 2 * c + 1] = (char)src[8 * x + 2 * order[c]];
				}
			}
		} else {
			row.assign((const char *)src, (size_t)w * 4);
		}
		if (fwrite(row.data(), 1, row.size(), o) != row.size()) {
			printf("SHIM short write\n");
			return 70;
		}
	}
	if (fclose(o) != 0) {
		printf("SHIM short write\n");
		return 70;
	}
	printf("OK %u %u 4 %d\n", w, h, wide ? 16 : 8);
	return 0;
}
