/* Row-at-a-time PAM output for decoder shims (impls/CONTRACT.md).
 *
 * A shim uses row-bounded buffers and writes each row at its output position as the decoder
 * produces it, without allocating another whole image. Non-interlaced rows arrive
 * whole and in order. An Adam7 pass row carries some of an image row's pixels: that image row
 * is read back from the output, the pixels placed, and the row written again. Rows the file
 * does not reach yet read as zeros.
 */
#ifndef PAM_ROWS_H
#define PAM_ROWS_H
/* pread/pwrite are POSIX, hidden under -std=c99 without this; so include this header first. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

/* The adapter's own invariants: not scored, a problem to fix. */
static inline void pam_assert(int ok, const char *message)
{
	if (!ok) {
		printf("SHIM %s\n", message);
		exit(70);
	}
}

/* The library returned without error but not an image the adapter can write: its result, a must-violation. */
static inline void pam_api(int ok, const char *message)
{
	if (!ok) {
		printf("API %s\n", message);
		exit(71);
	}
}

/* PNG 8.2 Adam7: starting column, starting row, column step, row step of passes 0-6. */
static const unsigned PAM_ADAM7[7][4] = {
	{0, 0, 8, 8}, {4, 0, 8, 8}, {0, 4, 4, 8}, {2, 0, 4, 4}, {0, 2, 2, 4}, {1, 0, 2, 2}, {0, 1, 1, 2}};

static inline unsigned pam_pass_cols(unsigned width, int pass)
{
	pam_assert(pass >= 0 && pass < 7, "invalid Adam7 pass");
	unsigned start = PAM_ADAM7[pass][0], step = PAM_ADAM7[pass][2];
	return width > start ? (width - start - 1) / step + 1 : 0;
}

static inline unsigned pam_pass_rows(unsigned height, int pass)
{
	pam_assert(pass >= 0 && pass < 7, "invalid Adam7 pass");
	unsigned start = PAM_ADAM7[pass][1], step = PAM_ADAM7[pass][3];
	return height > start ? (height - start - 1) / step + 1 : 0;
}

struct pam_out {
	int fd;
	uint64_t width, height;
	size_t bpp; /* bytes per pixel */
	size_t rowbytes;
	off_t body; /* offset of the first row */
};

static inline int pam_write_at(int fd, const unsigned char *buf, size_t n, off_t off)
{
	while (n) {
		ssize_t k = pwrite(fd, buf, n, off);
		if (k == 0) {
			errno = EIO;
			return -1;
		}
		if (k < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		buf += k;
		n -= (size_t)k;
		off += k;
	}
	return 0;
}

/* Reads n bytes at off; bytes past the end of the file read as zero. */
static inline int pam_read_at(int fd, unsigned char *buf, size_t n, off_t off)
{
	while (n) {
		ssize_t k = pread(fd, buf, n, off);
		if (k < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		if (k == 0) {
			memset(buf, 0, n);
			return 0;
		}
		buf += k;
		n -= (size_t)k;
		off += k;
	}
	return 0;
}

/* channels 1-4 (G, GA, RGB, RGBA); maxval 255 or 65535. */
static inline int pam_open(struct pam_out *p, const char *path, unsigned width, unsigned height, int channels, int maxval)
{
	pam_api(width && height, "image has a zero dimension");
	pam_assert(channels >= 1 && channels <= 4, "invalid PAM channels");
	pam_assert(maxval == 255 || maxval == 65535, "unexpected PAM sample precision");
	pam_assert(sizeof(off_t) >= 8, "PAM output requires 64-bit file offsets");
	static const char *const tupltype[] = {"GRAYSCALE", "GRAYSCALE_ALPHA", "RGB", "RGB_ALPHA"};
	char hdr[128];
	int n = snprintf(hdr, sizeof hdr, "P7\nWIDTH %u\nHEIGHT %u\nDEPTH %d\nMAXVAL %d\nTUPLTYPE %s\nENDHDR\n",
		width, height, channels, maxval, tupltype[channels - 1]);
	pam_assert(n > 0 && (size_t)n < sizeof hdr, "PAM header truncated");
	p->bpp = (size_t)channels * (maxval > 255 ? 2 : 1);
	pam_assert(width <= SIZE_MAX / p->bpp, "PAM row size overflow");
	p->rowbytes = (size_t)width * p->bpp;
	pam_assert(height <= ((uint64_t)INT64_MAX - (unsigned)n) / p->rowbytes, "PAM file offset overflow");
	p->fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
	if (p->fd < 0)
		return -1;
	if (pam_write_at(p->fd, (const unsigned char *)hdr, (size_t)n, 0)) {
		close(p->fd);
		return -1;
	}
	p->width = width;
	p->height = height;
	p->body = n;
	return 0;
}

/* Image row y, whole. */
static inline int pam_row(struct pam_out *p, uint64_t y, const unsigned char *row)
{
	pam_api(y < p->height, "more rows than the image height");
	return pam_write_at(p->fd, row, p->rowbytes, p->body + (off_t)(y * p->rowbytes));
}

/* Row `line` of Adam7 pass `pass`: its n pixels, packed, go to their columns of the image row.
 * `scratch` holds one image row. */
static inline int pam_pass_row(struct pam_out *p, int pass, uint64_t line, const unsigned char *pixels, unsigned n,
	unsigned char *scratch)
{
	pam_assert(pass >= 0 && pass < 7, "invalid Adam7 pass");
	pam_api(line < pam_pass_rows((unsigned)p->height, pass), "more Adam7 rows than the pass holds");
	pam_api(n == pam_pass_cols((unsigned)p->width, pass), "Adam7 row with the wrong pixel count");
	uint64_t y = PAM_ADAM7[pass][1] + line * PAM_ADAM7[pass][3];
	off_t off = p->body + (off_t)(y * p->rowbytes);
	if (pam_read_at(p->fd, scratch, p->rowbytes, off))
		return -1;
	for (unsigned i = 0; i < n; i++)
		memcpy(scratch + ((size_t)PAM_ADAM7[pass][0] + (size_t)i * PAM_ADAM7[pass][2]) * p->bpp,
			pixels + (size_t)i * p->bpp, p->bpp);
	return pam_write_at(p->fd, scratch, p->rowbytes, off);
}

/* Fails unless the file holds exactly the header and every row. */
static inline int pam_close(struct pam_out *p)
{
	off_t end = lseek(p->fd, 0, SEEK_END);
	int complete = end == p->body + (off_t)(p->height * p->rowbytes);
	return close(p->fd) == 0 && complete ? 0 : -1;
}
#endif
