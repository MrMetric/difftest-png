"""Pillow's default image, written without color management, a band of rows at a time.
Modes up to 8 bits go through convert("RGBA"), which applies palette and tRNS. 16-bit gray is
written as 16-bit without its tRNS key: Pillow's convert() drops the key for 16-bit modes and
has no 16-bit alpha mode, and the shim does not stand in for it.
Pillow reads 16-bit RGB(A) as 8-bit, so the column is registered max_depth 8.
"""
import struct
import sys
import warnings
from pamwriter import api, failure, header, run

try:
	import PIL
	from PIL import Image
except ImportError as e:
	print("ERR decoder library not installed:", e)
	sys.exit(127)

warnings.simplefilter("always")
BAND_BYTES = 1 << 24  # output bytes held at once: a band of rows, never the image


def main():
	if sys.argv[1:] == ["--version"]:
		print("Pillow", PIL.__version__)
		return
	src, dst = sys.argv[1:]
	try:
		im = Image.open(src)
	except Exception as e:
		failure(e, decoder=True)
	with im:
		try:
			im.load()
		except Exception as e:
			failure(e, decoder=True)
		w, h = im.size
		print(f"INFO mode={im.mode} size={w}x{h} info={','.join(sorted(im.info))}")
		wide = im.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N")
		api(wide or im.mode in ("1", "L", "LA", "P", "PA", "RGB", "RGBA"), f"unhandled mode {im.mode}")
		channels, maxval = (1, 65535) if wide else (4, 255)
		rowbytes = w * channels * (2 if wide else 1)
		band = max(1, min(h, BAND_BYTES // max(rowbytes, 1)))
		with open(dst, "wb") as out:
			header(out, w, h, channels, maxval)
			for y0 in range(0, h, band):
				y1 = min(h, y0 + band)
				with im.crop((0, y0, w, y1)) as rows:
					if rows.mode in ("I;16", "I;16B"):
						body = rows.tobytes("raw", "I;16B")
					elif wide:  # mode I: 32-bit samples, written when they fit 16 bits
						body = bytearray(rowbytes * (y1 - y0))
						samples = rows.load()
						i = 0
						for y in range(y1 - y0):
							for x in range(w):
								value = samples[x, y]
								api(0 <= value <= 65535, f"sample {value} outside 16 bits")
								struct.pack_into(">H", body, i, value)
								i += 2
					else:
						with rows.convert("RGBA") as rgba:
							body = rgba.tobytes()
					api(len(body) == rowbytes * (y1 - y0), f"{len(body)} bytes for {y1 - y0} rows of {rowbytes}")
					out.write(body)
		print(f"OK {w} {h} {channels} {16 if wide else 8}")


if __name__ == "__main__":
	run(main)
