"""OpenCV shim. Policy: cv2.imread with IMREAD_UNCHANGED (keeps 16-bit and
alpha, expands palette); BGR(A) reordered to RGB(A).
"""
import sys

try:
	import cv2
	import numpy as np
except ImportError as e:  # 127: the decoder library is not installed (see impls/CONTRACT.md)
	print("ERR decoder library not installed:", e)
	sys.exit(127)


def fail(msg):
	print("ERR", msg)
	sys.exit(1)


def main():
	if sys.argv[1:] == ["--version"]:
		png_line = next((l.strip() for l in cv2.getBuildInformation().splitlines() if l.strip().startswith("PNG:")), "")
		print(f"OpenCV {cv2.__version__} ({png_line})")
		return
	src, dst = sys.argv[1], sys.argv[2]
	img = cv2.imread(src, cv2.IMREAD_UNCHANGED)
	if img is None:
		fail("imread returned None")
	print(f"INFO shape={img.shape} dtype={img.dtype}")
	if img.ndim == 2:
		a, channels = img, 1
	elif img.shape[2] == 3:
		a, channels = img[:, :, ::-1], 3
	elif img.shape[2] == 4:
		a, channels = img[:, :, [2, 1, 0, 3]], 4
	elif img.shape[2] == 2:
		a, channels = img, 2
	else:
		fail(f"unhandled shape {img.shape}")
	if a.dtype == np.uint8:
		maxval, body = 255, np.ascontiguousarray(a).tobytes()
	elif a.dtype == np.uint16:
		maxval, body = 65535, a.astype(">u2").tobytes()
	else:
		fail(f"unhandled dtype {a.dtype}")
	h, w = a.shape[:2]
	tt = ["GRAYSCALE", "GRAYSCALE_ALPHA", "RGB", "RGB_ALPHA"][channels - 1]
	with open(dst, "wb") as f:
		f.write(f"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH {channels}\nMAXVAL {maxval}\nTUPLTYPE {tt}\nENDHDR\n".encode())
		f.write(body)
	print(f"OK {w} {h} {channels} {16 if maxval > 255 else 8}")


if __name__ == "__main__":
	main()
