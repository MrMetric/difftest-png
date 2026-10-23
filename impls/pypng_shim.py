"""pypng shim. Policy: Reader.asRGBA(), which applies palette and tRNS and
keeps 16-bit samples; bit depths below 8 come back scaled to 8 by pypng.
"""
import array
import sys

try:
	import png
except ImportError as e:  # 127: the decoder library is not installed (see impls/CONTRACT.md)
	print("ERR decoder library not installed:", e)
	sys.exit(127)


def fail(msg):
	print("ERR", msg)
	sys.exit(1)


def main():
	if sys.argv[1:] == ["--version"]:
		print("pypng", png.__version__)
		return
	src, dst = sys.argv[1], sys.argv[2]
	try:
		r = png.Reader(filename=src)
		w, h, rows, info = r.asRGBA()
		bd = info["bitdepth"]
		maxval = (1 << bd) - 1
		chunks = []
		for row in rows:
			a = array.array("H" if bd > 8 else "B", row) if not isinstance(row, array.array) else row
			if bd > 8 and sys.byteorder == "little":
				a = array.array("H", a)
				a.byteswap()
			chunks.append(a.tobytes())
	except Exception as e:  # noqa: BLE001
		fail(f"{type(e).__name__}: {e}")
	print(f"INFO width={w} height={h} bitdepth={bd} info={','.join(f'{k}={v}' for k, v in sorted(info.items()) if k in ('alpha','greyscale','interlace','palette','planes','gamma'))}")
	if len(chunks) != h:
		# pypng stops at the end of the image data without complaint: say so instead of writing a short PAM.
		fail(f"pypng returned {len(chunks)} of {h} rows without raising")
	with open(dst, "wb") as f:
		f.write(f"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH 4\nMAXVAL {maxval}\nTUPLTYPE RGB_ALPHA\nENDHDR\n".encode())
		f.writelines(chunks)
	print(f"OK {w} {h} 4 {16 if bd > 8 else 8}")


if __name__ == "__main__":
	main()
