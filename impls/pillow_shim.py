"""Pillow shim. Policy: Image.open + load, then whatever mode Pillow chose is
written: 16-bit grey modes as 16-bit grey; every mode up to 8 bits through
convert("RGBA"), which applies palette and the transparency info exactly.
Pillow reads 16-bit RGB(A) as 8-bit, so the column is registered max_depth 8.
"""
import struct
import sys
import warnings

try:
	import PIL
	from PIL import Image
except ImportError as e:  # 127: the decoder library is not installed (see impls/CONTRACT.md)
	print("ERR decoder library not installed:", e)
	sys.exit(127)

warnings.simplefilter("always")


def fail(msg):
	print("ERR", msg)
	sys.exit(1)


def main():
	if sys.argv[1:] == ["--version"]:
		print("Pillow", PIL.__version__)
		return
	src, dst = sys.argv[1], sys.argv[2]
	try:
		im = Image.open(src)
		im.load()
	except Exception as e:  # noqa: BLE001 - any failure is the decoder's verdict
		fail(f"{type(e).__name__}: {e}")
	w, h = im.size
	print(f"INFO mode={im.mode} size={w}x{h} info={','.join(sorted(im.info))}")
	if im.mode in ("I;16", "I;16B", "I;16L", "I;16N"):
		body = im.tobytes("raw", "I;16B")
		channels, maxval = 1, 65535
	elif im.mode == "I":
		vals = list(im.getdata())
		if max(vals, default=0) > 65535 or min(vals, default=0) < 0:
			fail("mode I sample outside 16-bit range")
		body = struct.pack(f">{len(vals)}H", *vals)
		channels, maxval = 1, 65535
	elif im.mode in ("1", "L", "LA", "P", "PA", "RGB", "RGBA"):
		body = im.convert("RGBA").tobytes()
		channels, maxval = 4, 255
	else:
		fail(f"unhandled mode {im.mode}")
	tt = {1: "GRAYSCALE", 4: "RGB_ALPHA"}[channels]
	with open(dst, "wb") as f:
		f.write(f"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH {channels}\nMAXVAL {maxval}\nTUPLTYPE {tt}\nENDHDR\n".encode())
		f.write(body)
	print(f"OK {w} {h} {channels} {16 if maxval > 255 else 8}")


if __name__ == "__main__":
	main()
