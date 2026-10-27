"""The picture every synthesized test shows, derived to each colour type and bit depth.

One picture, 13 by 15 pixels, defined once as a function of position with RGBA samples in [0, 1],
and quantized from that to every colour type and bit depth of IHDR's Table 12. The tests then differ
only in representation, and a reader can see that each file shows the same thing, within what its
colour type and depth can hold.

What the picture contains, and why:

- A sky grading from deep blue at the top to orange at the horizon: a vertical gradient with a
  different slope in each channel, so every filter type has something to predict, channel order
  shows, and 16-bit samples carry low-order bits that an 8-bit reduction has to round or truncate.
- A sun at the left, with a soft edge and pure white at its centre: a flat region, a few blended
  edge pixels, and the maximum sample value.
- Ground below, a horizontal gradient whose rows repeat: the Up filter predicts exactly and Paeth
  ties, and the rows match each other for the DEFLATE tests.
- Two black pixels at the bottom left: with the sun's centre, pure black and pure white both occur,
  so a tRNS naming 0 or the maximum sample names a pixel at every depth.
- Alpha, in the colour types that have it: opaque except the four rightmost columns, which fade to
  fully transparent at the right edge. The transparent column keeps its colour, so what a decoder
  does with colour under alpha 0 can be seen. Crops taken from the top left are opaque.

13 is the smallest width and height at which every Adam7 pass has at least two columns and two rows,
and 13 leaves padding bits in every row at every sub-byte depth. The height is 15 rather than 13 so
that the picture is not square and a decoder that swaps width and height is caught.

Greyscale is the picture's luminance (Rec. 709 weights). The 8-bit palette holds the picture's
colours, of which there are fewer than 256; lower palette depths are a median cut of those. Alpha is
carried by the greyscale+alpha and truecolour+alpha types only; the other types are opaque, and a
tRNS is added by the tests that are about it.
"""
from dataclasses import dataclass

W, H = 13, 15
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
CT_NAME = {0: "g", 2: "rgb", 3: "p", 4: "ga", 6: "rgba"}
DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}  # Table 12
COMBOS = [(ct, bd) for ct, bds in DEPTHS.items() for bd in bds]

SKY_TOP = (0.05, 0.12, 0.50)
SKY_HORIZON = (0.95, 0.52, 0.12)
SUN_EDGE = (1.0, 0.86, 0.40)
SUN_CENTRE = (1.0, 1.0, 1.0)
SUN_X, SUN_Y, SUN_R = 4.0, 4.0, 2.4
GROUND_LEFT = (0.10, 0.38, 0.08)
GROUND_RIGHT = (0.48, 0.78, 0.26)
HORIZON = 10  # first ground row
ROCK = ((1, 14), (2, 14))
FADE = 4  # columns over which alpha falls to 0 at the right edge


RGB = tuple[float, float, float]


def lerp(a: RGB, b: RGB, t: float) -> RGB:
	return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def sun_coverage(x: int, y: int) -> float:
	"""The fraction of pixel (x, y) inside the sun's disc, from a 4x4 grid of sample points."""
	inside = 0
	for j in range(4):
		for i in range(4):
			dx, dy = x + (i + 0.5) / 4 - 0.5 - SUN_X, y + (j + 0.5) / 4 - 0.5 - SUN_Y
			inside += dx * dx + dy * dy <= SUN_R * SUN_R
	return inside / 16


def picture(x: int, y: int) -> tuple[float, float, float, float]:
	"""The picture at (x, y): (r, g, b, a), each in [0, 1]."""
	assert 0 <= x < W and 0 <= y < H, (x, y)
	if (x, y) in ROCK:
		rgb = (0.0, 0.0, 0.0)
	elif y >= HORIZON:
		rgb = lerp(GROUND_LEFT, GROUND_RIGHT, x / (W - 1))
	else:
		rgb = lerp(SKY_TOP, SKY_HORIZON, y / (HORIZON - 1))
		cover = sun_coverage(x, y)
		if cover:
			d = ((x - SUN_X) ** 2 + (y - SUN_Y) ** 2) ** 0.5
			sun = lerp(SUN_CENTRE, SUN_EDGE, min(d / SUN_R, 1.0))
			rgb = lerp(rgb, sun, cover)
	alpha = min(1.0, (W - 1 - x) / FADE)
	return (rgb[0], rgb[1], rgb[2], alpha)


def luminance(r: float, g: float, b: float) -> float:
	return 0.2126 * r + 0.7152 * g + 0.0722 * b


def quantize(v: float, bits: int) -> int:
	m = (1 << bits) - 1
	assert 0.0 <= v <= 1.0, v
	return int(v * m + 0.5)


@dataclass(frozen=True)
class Variant:
	"""The picture at one colour type and depth: `rows` holds each row's samples, channels interleaved."""
	colour_type: int
	bit_depth: int
	width: int
	height: int
	rows: tuple[tuple[int, ...], ...]
	palette: tuple[tuple[int, int, int], ...] | None  # colour type 3 only

	@property
	def name(self) -> str:
		return f"{CT_NAME[self.colour_type]}{self.bit_depth}"

	def rgba16(self) -> list[list[tuple[int, int, int, int]]]:
		"""The pixels a decoder must produce, as RGBA with 16 bits per sample (every maxval divides 65535)."""
		k = 65535 // ((1 << self.bit_depth) - 1)
		out = []
		for row in self.rows:
			px = []
			for i in range(self.width):
				match self.colour_type:
					case 0:
						g = row[i] * k
						px.append((g, g, g, 65535))
					case 2:
						r, g, b = row[3 * i:3 * i + 3]
						px.append((r * k, g * k, b * k, 65535))
					case 3:
						assert self.palette is not None
						r, g, b = self.palette[row[i]]
						px.append((r * 257, g * 257, b * 257, 65535))
					case 4:
						g, a = row[2 * i:2 * i + 2]
						px.append((g * k, g * k, g * k, a * k))
					case 6:
						r, g, b, a = row[4 * i:4 * i + 4]
						px.append((r * k, g * k, b * k, a * k))
			out.append(px)
		return out


def median_cut(pixels: list[tuple[int, int, int]], n: int) -> list[list[tuple[int, int, int]]]:
	"""Split the pixels into n boxes: repeatedly halve, at its median, the box with the widest channel range."""
	boxes = [list(pixels)]
	while len(boxes) < n:
		spans = [(max(max(p[c] for p in b) - min(p[c] for p in b) for c in range(3)), -i) for i, b in enumerate(boxes)]
		span, neg_i = max(spans)
		assert span > 0, "fewer distinct colours than palette entries"
		box = boxes[-neg_i]
		axis = max(range(3), key=lambda c: max(p[c] for p in box) - min(p[c] for p in box))
		box.sort(key=lambda p: (p[axis], p))
		mid = len(box) // 2
		boxes[-neg_i], boxes[len(boxes):] = box[:mid], [box[mid:]]
	return boxes


def mean(box: list[tuple[int, int, int]], c: int) -> int:
	return int(sum(p[c] for p in box) / len(box) + 0.5)


def variant(colour_type: int, bit_depth: int, width: int = W, height: int = H) -> Variant:
	"""The picture's top-left `width` by `height` at this colour type and depth."""
	assert bit_depth in DEPTHS[colour_type], (colour_type, bit_depth)
	assert 0 < width <= W and 0 < height <= H, (width, height)
	pix = [[picture(x, y) for x in range(width)] for y in range(height)]
	palette = None
	match colour_type:
		case 0:
			rows = [[quantize(luminance(*p[:3]), bit_depth) for p in row] for row in pix]
		case 2:
			rows = [[quantize(v, bit_depth) for p in row for v in p[:3]] for row in pix]
		case 4:
			rows = [[quantize(v, bit_depth) for p in row for v in (luminance(*p[:3]), p[3])] for row in pix]
		case 6:
			rows = [[quantize(v, bit_depth) for p in row for v in p] for row in pix]
		case 3:
			# The palette comes from the whole picture, so a crop keeps the full-size variant's indices.
			whole = [[picture(x, y) for x in range(W)] for y in range(H)]
			rgb8 = [[(quantize(p[0], 8), quantize(p[1], 8), quantize(p[2], 8)) for p in row] for row in whole]
			if bit_depth == 8:
				entries = list(dict.fromkeys(p for row in rgb8 for p in row))  # the picture's colours, in raster order
				assert len(entries) <= 256, len(entries)
				index = {p: i for i, p in enumerate(entries)}
			else:
				boxes = median_cut([p for row in rgb8 for p in row], 1 << bit_depth)
				entries = [(mean(b, 0), mean(b, 1), mean(b, 2)) for b in boxes]
				assert len(set(entries)) == len(entries), "two palette entries alike"
				index = {p: i for i, b in enumerate(boxes) for p in b}
			rows = [[index[p] for p in row[:width]] for row in rgb8[:height]]
			palette = tuple(entries)
			assert set(index.values()) == set(range(len(entries))), "an unused palette entry in the whole picture"
		case _:
			raise AssertionError(colour_type)
	return Variant(colour_type, bit_depth, width, height, tuple(tuple(r) for r in rows), palette)
