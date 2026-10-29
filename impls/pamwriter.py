"""Row output and failure attribution shared by the Python adapters.

Three exits besides success: a decoder error (ERR, 1), a library result that is not an image the
adapter can write (API, 71), the adapter's own failure (SHIM, 70).
"""


class ApiError(Exception):
	"""The library returned without error, but not an image: wrong size, missing rows, non-numeric samples."""


def api(ok, message):
	if not ok:
		raise ApiError(message)


def header(out, width, height, channels, maxval):
	api(width > 0 and height > 0, f"invalid image dimensions {width}x{height}")
	assert 1 <= channels <= 4 and 1 <= maxval <= 65535, f"invalid output channels/precision: {channels}/{maxval}"
	tt = ("GRAYSCALE", "GRAYSCALE_ALPHA", "RGB", "RGB_ALPHA")[channels - 1]
	out.write(f"P7\nWIDTH {width}\nHEIGHT {height}\nDEPTH {channels}\nMAXVAL {maxval}\nTUPLTYPE {tt}\nENDHDR\n".encode())


def failure(error, *, decoder):
	parts, seen = [], set()
	while error is not None and id(error) not in seen:
		seen.add(id(error))
		parts.append(f"{type(error).__name__}: {error}")
		error = error.__cause__ or error.__context__
	print("ERR" if decoder else "SHIM", " <- ".join(parts).replace("\n", " ").replace("\r", " "))
	raise SystemExit(1 if decoder else 70)


def run(main):
	try:
		main()
	except ApiError as error:
		print("API", str(error).replace("\n", " "))
		raise SystemExit(71)
	except Exception as error:
		failure(error, decoder=False)
