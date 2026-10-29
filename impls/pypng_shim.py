"""PyPNG Reader.asRGBA(), consuming its lazy rows through completion.
Its returned bit depth (including release sBIT reduction) is carried by PAM.
"""
import array
import sys
from pamwriter import ApiError, api, failure, header, run

try:
	import png
except ImportError as e:
	print("ERR decoder library not installed:", e)
	sys.exit(127)


def samples(row, bd):
	"""One row's samples as big-endian PAM bytes; a non-integer or a value past the depth is an api failure."""
	try:
		a = array.array("H" if bd > 8 else "B", row)
	except (TypeError, OverflowError, ValueError) as e:
		raise ApiError(f"row samples: {type(e).__name__}: {e}") from None
	if bd not in (8, 16):  # the array type bounds those two; sBIT reduction yields depths between
		api(max(a, default=0) <= (1 << bd) - 1, f"sample {max(a)} past bit depth {bd}")
	if bd > 8 and sys.byteorder == "little":
		a.byteswap()
	return a.tobytes()


def main():
	if sys.argv[1:] == ["--version"]:
		print("pypng", png.__version__)
		return
	src, dst = sys.argv[1:]
	with open(src, "rb") as source:
		try:
			w, h, rows, info = png.Reader(file=source).asRGBA()
			rows = iter(rows)
		except Exception as e:
			failure(e, decoder=True)
		bd = info["bitdepth"]
		print(f"INFO width={w} height={h} bitdepth={bd} planes={info['planes']}")
		transport_error = None
		with open(dst, "wb") as out:
			try:
				api(1 <= bd <= 16, f"bit depth {bd}")
				header(out, w, h, 4, (1 << bd) - 1)
				y = 0
				while True:
					try:
						row = next(rows)
					except StopIteration:
						break
					except Exception as e:
						failure(e, decoder=True)
					api(y < h, f"pypng returned more than {h} rows")
					api(len(row) == w * 4, f"row {y} holds {len(row)} samples, not {w * 4}")
					out.write(samples(row, bd))
					y += 1
				api(y == h, f"pypng returned {y} of {h} rows without raising")
			except ApiError as e:
				transport_error = e
		if transport_error is not None:
			# An unusable row/header must not hide a later error from the lazy decoder.
			try:
				for _ in rows:
					pass
			except Exception as e:
				failure(e, decoder=True)
			raise transport_error
	print(f"OK {w} {h} 4 {16 if bd > 8 else 8}")


if __name__ == "__main__":
	run(main)
