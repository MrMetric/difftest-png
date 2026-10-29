"""OpenCV IMREAD_UNCHANGED; native arrays serialized in RGB order by row."""
import sys
from pamwriter import api, failure, header, run

try:
	import cv2
	import numpy as np
except ImportError as e:
	print("ERR decoder library not installed:", e)
	sys.exit(127)


def main():
	if sys.argv[1:] == ["--version"]:
		png_line = next((l.strip() for l in cv2.getBuildInformation().splitlines() if l.strip().startswith("PNG:")), "")
		print(f"OpenCV {cv2.__version__} ({png_line})")
		return
	src, dst = sys.argv[1:]
	try:
		img = cv2.imread(src, cv2.IMREAD_UNCHANGED)
		if img is None:
			raise ValueError("imread returned None")
	except Exception as e:
		failure(e, decoder=True)
	print(f"INFO shape={img.shape} dtype={img.dtype}")
	api(img.ndim in (2, 3), f"unhandled array shape {img.shape}")
	channels = 1 if img.ndim == 2 else img.shape[2]
	api(1 <= channels <= 4 and img.dtype in (np.uint8, np.uint16), f"unhandled array {img.shape} {img.dtype}")
	maxval = 65535 if img.dtype == np.uint16 else 255
	h, w = img.shape[:2]
	with open(dst, "wb") as out:
		header(out, w, h, channels, maxval)
		for row in img:
			if channels == 3:
				row = row[:, ::-1]
			elif channels == 4:
				row = row[:, [2, 1, 0, 3]]
			body = row.astype(">u2").tobytes() if maxval > 255 else row.tobytes()
			api(len(body) == w * channels * (2 if maxval > 255 else 1), f"row of {len(body)} bytes for {w} pixels")
			out.write(body)
	print(f"OK {w} {h} {channels} {16 if maxval > 255 else 8}")


if __name__ == "__main__":
	run(main)
