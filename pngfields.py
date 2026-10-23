"""Declarative PNG fields and the only parser of known chunk bodies.

A layout consumes its entire input. Handlers receive immutable named values, never the
input buffer. Only Fragment fields expose bytes, for IDAT/fdAT compressed image data.
Content fields require a framing validator; there is no unchecked metadata payload type.

Every input check explicitly chooses VALUE or LAYOUT. Value faults invalidate the field
but leave later independent fields readable. Layout faults end the current scope. An
unusable dependency is skipped only when its extent is independently known; otherwise
the scope ends without guessing a boundary or adding a diagnostic. Embedded content has
its own bounded scope. No faulted aggregate is delivered to a handler.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
import re
import struct

import inflate
from pngdiagnostics import StreamDiagnostics


class Ignored(Exception):
	"""The diagnostic is already recorded; do not dispatch this ancillary chunk."""


class Fault(Enum):
	VALUE = auto()
	LAYOUT = auto()


class _BrokenLayout(Exception):
	"""A recorded layout fault ends the current parsing scope."""


class _Unavailable(Exception):
	"""A dependency failed validation; its stored bits are not a usable value."""


class _Unlocatable(Exception):
	"""An unusable dependency prevents locating the next independent field."""


_UNUSABLE = object()


class _Fields(Mapping):
	"""Dependency access never exposes an invalid value, including through get()/values()."""
	def __init__(self):
		self.__data = {}

	def __getitem__(self, key):
		value = self.__data[key]
		if value is _UNUSABLE:
			raise _Unavailable(key)
		return value

	def __iter__(self):
		return iter(self.__data)

	def __len__(self):
		return len(self.__data)

	def store(self, key, value):
		self.__data[key] = value


class Values:
	__slots__ = ("__values",)

	def __init__(self, values):
		object.__setattr__(self, "_Values__values", MappingProxyType(dict(values)))

	def __getattr__(self, name):
		try:
			return self.__values[name]
		except KeyError:
			raise AttributeError(name) from None

	def __setattr__(self, name, value):
		raise AttributeError("parsed fields are immutable")

	def tuple(self):
		return tuple(self.__values.values())


@dataclass(frozen=True)
class UInt:
	width: int
	clause: str | None = None
	allowed: object = None

	def __post_init__(self):
		assert self.width in (1, 2, 4), self.width
		assert self.allowed is None or self.clause, "restricted values need a clause"


# 7.1 "PNG four-byte unsigned integers are limited to the range 0 to 2^31-1".
# Width 4 ALWAYS enforces uint31; the clause can specialize the diagnostic, never the bound.
BYTE = UInt(1)
UINT16 = UInt(2)
UINT31 = UInt(4, "7.1#uint31")


@dataclass(frozen=True)
class Sample:
	"""A two-byte PNG sample whose unused high bits are masked under both editions."""
	mask_clause: str


def enum(values, clause):
	return UInt(1, clause, tuple(values))


@dataclass(frozen=True)
class Repeat:
	item: object
	count: object = None  # None means all remaining complete entries; callable uses parser context
	clause: str | None = None
	minimum: int = 0
	maximum: int | None = None

	def __post_init__(self):
		assert self.minimum >= 0 and (self.maximum is None or self.maximum >= self.minimum)
		assert self.count is None or callable(self.count) or isinstance(self.count, int) and self.count >= 0


@dataclass(frozen=True)
class Struct:
	fields: tuple

	def __post_init__(self):
		assert len({name for name, _ in self.fields}) == len(self.fields), "duplicate field names"


@dataclass(frozen=True)
class Choice:
	select: object
	options: dict

	def __post_init__(self):
		assert callable(self.select) and self.options
		object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True)
class Text:
	kind: str  # keyword, latin1, utf8, language
	terminated: bool = False
	clause: str | None = None
	null_clause: str | None = None

	def __post_init__(self):
		assert self.kind in ("keyword", "latin1", "utf8", "language"), self.kind
		assert self.kind not in ("latin1", "utf8") or self.clause, "encoded text needs a diagnostic clause"


@dataclass(frozen=True)
class Compressed:
	content: object
	clause: str
	flag: str | None = None
	method: str = "method"


@dataclass(frozen=True)
class Content:
	kind: str  # ICC or Exif: the bounded framing grammar is validated before delivery

	def __post_init__(self):
		assert self.kind in ("ICC", "Exif"), "content needs an implemented framing grammar"


@dataclass(frozen=True)
class Fragment:
	"""A fragment of a compressed image stream, interpreted after concatenation."""


@dataclass(frozen=True)
class RemainderLength:
	"""Discard bytes and return only their count (invalid IEND data)."""


@dataclass(frozen=True)
class Layout:
	fields: Struct
	length_clause: str = "13.2#known-length"


class Parser:
	def __init__(self, d, name: bytes, body: bytes, layout: Layout, fatal=False):
		self.d, self.name, self.data, self.layout, self.fatal = d, name, body, layout, fatal
		self.pos = 0
		self.values = _Fields()

	def fail(self, id, message, *, kind: Fault):
		assert isinstance(kind, Fault), "every check must classify its fault"
		message = f"{self.name.decode()}: {message}"
		if self.fatal:
			self.d.require(False, id, message)
		if self.name in (b"acTL", b"fcTL", b"fdAT"):
			# 13.1 "A decoder which detects an error before the animation has started should display the static image."
			self.d.reach("13.1#apng-fallback")
		self.d.expect(False, id, message + "; ignored")
		if kind is Fault.LAYOUT:
			raise _BrokenLayout

	def check(self, ok, id, message, *, kind: Fault):
		assert isinstance(kind, Fault), "every check must classify its fault"
		if not ok:
			self.fail(id, message, kind=kind)
		return bool(ok)

	def take(self, size):
		assert size >= 0, size
		self.check(self.pos + size <= len(self.data), self.layout.length_clause, "field extends beyond chunk data", kind=Fault.LAYOUT)
		value = self.data[self.pos:self.pos + size]
		self.pos += size
		return value

	def resolve(self, value):
		return value(self) if callable(value) else value

	def size(self, field):
		match field:
			case UInt(width=width):
				return width
			case Sample():
				return 2
			case Struct(fields=fields):
				total = 0
				for _, f in fields:
					part = self.size(f)
					if part is None:
						return None
					total += part
				return total
			case Repeat(item=item, count=count) if count is not None:
				size = self.size(item)
				try:
					count = self.resolve(count)
				except (KeyError, _Unavailable):
					# A count stored in an earlier field is known only while reading this structure.
					return None
				return None if size is None else size * count
			case Choice(options=options):
				# A selector can depend on an earlier field that has not been read yet.
				sizes = {self.size(option) for option in options.values()}
				return sizes.pop() if len(sizes) == 1 else None
		return None

	def check_length(self, length):
		"""Check framing knowable from the declared length, without reading any field value."""
		start = self.d.diagnostics.fault_count
		try:
			size = self.size(self.layout.fields)
			if size is not None:
				# 13.2 "For known-length chunks ... treat an unexpected chunk length as an error."
				self.check(size == length, self.layout.length_clause, f"length {length}, expected {size}", kind=Fault.LAYOUT)
			elif self.fatal and len(self.layout.fields.fields) == 1:
				field = self.layout.fields.fields[0][1]
				if isinstance(field, Repeat) and field.count is None:
					self.entry_count(field, length)
		except _BrokenLayout:
			return False
		return self.d.diagnostics.fault_count == start

	def entry_count(self, field, remaining):
		size = self.size(field.item)
		assert size is not None and size > 0, "repeated entries must have a positive fixed size"
		clause = field.clause or self.layout.length_clause
		if field.count is None:
			self.check(remaining % size == 0, clause, f"{remaining} bytes are not complete {size}-byte entries", kind=Fault.LAYOUT)
			count = remaining // size
		else:
			count = self.resolve(field.count)
		self.check(count >= field.minimum and (field.maximum is None or count <= field.maximum),
			clause, f"entry count {count} is outside the allowed range", kind=Fault.VALUE)
		self.check(count * size <= remaining, clause, "entry count exceeds the data", kind=Fault.LAYOUT)
		return count

	def parse(self):
		start = self.d.diagnostics.fault_count
		if not self.check_length(len(self.data)):
			raise Ignored
		try:
			result = self.read(self.layout.fields)
			self.check(self.pos == len(self.data), self.layout.length_clause, "bytes remain after the declared fields", kind=Fault.LAYOUT)
		except (_BrokenLayout, _Unlocatable):
			raise Ignored from None
		if result is _UNUSABLE or self.d.diagnostics.fault_count != start:
			raise Ignored
		return result

	def remainder(self, field):
		"""Whether this type's extent is the rest of the enclosing buffer, independent of its value."""
		match field:
			case Text(terminated=False) | Compressed() | Content() | Fragment() | RemainderLength() | Repeat(count=None):
				return True
			case Choice(options=options):
				return all(self.remainder(option) for option in options.values())
		return False

	def skip(self, field):
		"""Skip unusable dependents only when their extent is independently known."""
		size = self.size(field)
		if size is not None:
			self.take(size)
		elif self.remainder(field):
			self.take(len(self.data) - self.pos)
		else:
			raise _Unlocatable
		return _UNUSABLE

	def read(self, field):
		start = self.d.diagnostics.fault_count
		pos = self.pos
		try:
			value = self._read(field)
		except _Unavailable:
			assert self.pos == pos, "resolve dependencies before consuming their field"
			return self.skip(field)
		return _UNUSABLE if self.d.diagnostics.fault_count != start else value

	def _read(self, field):
		match field:
			case Sample(mask_clause=clause):
				value = self.read(UINT16)
				# 11.3.1.1 / 11.3.4.1, both editions: "the least significant bits are used";
				# edition 3: "decoders must mask the other bits to 0 before the value is used".
				mask = (1 << self.d.depth) - 1
				if value > mask:
					self.d.reach(clause)
				return value & mask
			case UInt(width=width, clause=clause, allowed=allowed):
				allowed = self.resolve(allowed)
				value = int.from_bytes(self.take(width), "big")
				if width == 4:
					self.check(value <= 0x7fffffff, clause or "7.1#uint31", f"four-byte unsigned integer {value} exceeds 2^31-1", kind=Fault.VALUE)
				if allowed is not None:
					self.check(value in allowed, clause, f"value {value} outside {allowed}", kind=Fault.VALUE)
				return value
			case Struct(fields=fields):
				values = _Fields()
				outer = self.values
				self.values = values
				usable = True
				try:
					for name, item in fields:
						value = self.read(item)
						values.store(name, value)
						usable &= value is not _UNUSABLE
				finally:
					self.values = outer
				return Values(values) if usable else _UNUSABLE
			case Choice(select=select, options=options):
				return self.read(options[select(self)])
			case Repeat(item=item):
				count = self.entry_count(field, len(self.data) - self.pos)
				values = tuple(self.read(item) for _ in range(count))
				return _UNUSABLE if any(value is _UNUSABLE for value in values) else values
			case Text():
				if field.terminated:
					end = self.data.find(b"\0", self.pos)
					self.check(end >= 0, self.layout.length_clause, "missing null separator", kind=Fault.LAYOUT)
					data = self.take(end - self.pos)
					self.take(1)
				else:
					data = self.take(len(self.data) - self.pos)
				return self.text(data, field)
			case Compressed(content=content, clause=clause, flag=flag, method=method):
				compressed = flag is None or self.values[flag]
				if compressed:
					assert self.values[method] == 0, "compression method must be validated before interpretation"
				data = self.take(len(self.data) - self.pos)
				compressed_size = len(data)
				if compressed:
					try:
						data, end = inflate.zlib_decompress(data, StreamDiagnostics(self.d.diagnostics, self.name.decode(), clause))
					except inflate.InflateError:
						# This is a value fault of the bounded payload. The stream's diagnostic is recorded;
						# failed decompression provides no content to validate.
						return _UNUSABLE
					# 10.3: each ancillary compressed payload is one complete zlib datastream.
					self.check(end == compressed_size, clause, "bytes follow the compressed datastream", kind=Fault.LAYOUT)
				child = Parser(self.d, self.name, data, Layout(Struct((("value", content),)), clause), self.fatal)
				try:
					return child.parse().value
				except Ignored:
					return _UNUSABLE
			case Content(kind=kind):
				data = self.take(len(self.data) - self.pos)
				try:
					return {"ICC": _icc, "Exif": _exif}[kind](self, data)
				except _BrokenLayout:
					# Embedded layout faults invalidate this bounded content, not its enclosing fields.
					return _UNUSABLE
			case Fragment():
				assert self.name in (b"IDAT", b"fdAT"), "metadata cannot opt out of content validation"
				return self.take(len(self.data) - self.pos)
			case RemainderLength():
				assert self.name == b"IEND", "only IEND discards its content"
				return len(self.take(len(self.data) - self.pos))
			case _:
				raise AssertionError(f"no parser for field {field!r}")

	def text(self, data, field):
		if field.kind == "keyword":
			# 11.3.3.1 "Keywords are restricted to 1 to 79 bytes in length."
			self.check(1 <= len(data) <= 79, "11.3.3.1#keyword-length", f"keyword length {len(data)}", kind=Fault.VALUE)
			# "only code points 0x20-7E and 0xA1-FF are allowed."
			self.check(all(32 <= b <= 126 or b >= 161 for b in data), "11.3.3.1#keyword-latin1", "keyword is not printable Latin-1", kind=Fault.VALUE)
			# "leading spaces, trailing spaces, and consecutive spaces are not permitted in keywords"
			self.check(not (data.startswith(b" ") or data.endswith(b" ") or b"  " in data), "11.3.3.1#keyword-spaces", "keyword spacing", kind=Fault.VALUE)
			return data.decode("latin-1")
		if field.null_clause:
			# 11.3.3: "Neither ... nor the text shall contain a zero byte (null character)."
			self.check(b"\0" not in data, field.null_clause, "text contains a null character", kind=Fault.VALUE)
		if field.kind == "language":
			# 11.3.3.4 "well-formed language tag defined by [BCP47]"; edition 2 "defined by [RFC-3066]".
			value = data.decode("latin-1")
			self.check(not value or LANGUAGE_TAG[self.d.edition].fullmatch(value) is not None,
				"11.3.3.4#language", f"language tag {value!r} does not match the edition's grammar", kind=Fault.VALUE)
			return value
		if field.kind == "latin1":
			# 11.3.3.1 "restricted to the printable Latin-1 character set plus U+000A LINE FEED"
			self.check(all(b == 10 or 32 <= b <= 126 or b >= 160 for b in data), field.clause, "text is not printable Latin-1 and LF", kind=Fault.VALUE)
			return data.decode("latin-1")
		assert field.kind == "utf8", field.kind
		# 11.3.3.4 "The translated keyword and text both use the UTF-8 encoding [rfc3629]".
		try:
			return data.decode("utf-8")
		except UnicodeDecodeError:
			self.fail(field.clause, "text is not UTF-8", kind=Fault.VALUE)


# RFC 5646 2.1 ABNF: Language-Tag = langtag / privateuse / grandfathered.
# The whole input must match; extension and private-use productions require content.
_PRIVATE = r"x(?:-[a-z0-9]{1,8})+"
_GRANDFATHERED = ("en-gb-oed|i-ami|i-bnn|i-default|i-enochian|i-hak|i-klingon|i-lux|i-mingo|i-navajo|i-pwn|i-tao|i-tay|"
	"i-tsu|sgn-be-fr|sgn-be-nl|sgn-ch-de|art-lojban|cel-gaulish|no-bok|no-nyn|zh-guoyu|zh-hakka|zh-min|zh-min-nan|zh-xiang")
_LANGTAG = (r"(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}|[a-z]{4}|[a-z]{5,8})(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?"
	r"(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*(?:-[0-9a-wyz](?:-[a-z0-9]{2,8})+)*(?:-" + _PRIVATE + ")?")
# RFC 3066 2.1: Primary-subtag = 1*8ALPHA; Subtag = 1*8(ALPHA / DIGIT).
LANGUAGE_TAG = {3: re.compile(f"{_LANGTAG}|{_PRIVATE}|{_GRANDFATHERED}", re.I | re.A),
	2: re.compile(r"[a-z]{1,8}(?:-[a-z0-9]{1,8})*", re.I | re.A)}


def _icc(p, data):
	"""ICC.1 sections 7.2/7.3: header, tag directory and all tag data extents."""
	check = lambda ok, msg, *, kind: p.check(ok, "11.3.2.3#profile", msg, kind=kind)
	# PNG 11.3.2.3: decompression "yields the embedded ICC profile".
	check(len(data) >= 132, "ICC header and tag count require at least 132 bytes", kind=Fault.LAYOUT)
	# ICC.1 7.2.2: "shall be the exact size"; 7.2.9: "shall contain the value 'acsp'".
	check(int.from_bytes(data[:4], "big") == len(data), "ICC declared size differs from profile length", kind=Fault.VALUE)
	check(data[36:40] == b"acsp", "ICC file signature is not acsp", kind=Fault.VALUE)
	# 7.3.1: "shall immediately follow the profile header".
	count = int.from_bytes(data[128:132], "big")
	table_end = 132 + 12 * count
	check(table_end <= len(data), "ICC tag table exceeds profile bounds", kind=Fault.LAYOUT)
	extents, signatures = [], set()
	for i in range(count):
		sig, offset, size = struct.unpack_from(">4sII", data, 132 + 12 * i)
		# ICC.1 7.3.1 prohibits "Duplicate tag signatures".
		check(sig not in signatures, f"ICC tag signature {sig!r} occurs twice", kind=Fault.VALUE)
		signatures.add(sig)
		# 7.3.4/7.3.5: offset and size describe the tag data element; 7.1.2: "4-byte boundary".
		if check(offset >= table_end and offset % 4 == 0 and size >= 8 and offset + size <= len(data),
			f"ICC tag {sig!r} has invalid extent {offset}+{size}", kind=Fault.VALUE):
			extents.append((offset, offset + size))
	# 7.1.2 permits multiple tags to reference the same data element, not partially overlapping elements.
	extents = sorted(set(extents))
	check(all(a[1] <= b[0] for a, b in zip(extents, extents[1:])), "ICC tag elements overlap", kind=Fault.VALUE)
	return Values({"colour_space": data[16:20].decode("latin-1"), "tags": count})


def _exif(p, data):
	"""TIFF framing: all IFD links, directory entries, and out-of-line value extents.

	Exif/GPS/Interop pointers and TIFF SubIFDs are directories. Vendor MakerNotes are
	opaque TIFF UNDEFINED values: their enclosing extent is checked, not vendor semantics.
	"""
	# PNG 11.3.4.5.2: "49 49 2A 00 ... or 4D 4D 00 2A".
	p.check(len(data) >= 4, "11.3.4.5#header", "not a TIFF byte-order and magic header", kind=Fault.LAYOUT)
	header_ok = p.check(data[:4] in (b"II*\0", b"MM\0*"), "11.3.4.5#header", "not a TIFF byte-order and magic header", kind=Fault.VALUE)
	check = lambda ok, msg, *, kind: p.check(ok, "11.3.4.5#structure", msg, kind=kind)
	# PNG: "contains an Exif profile"; TIFF 6.0 section 2: header, IFD count, entries, next-IFD offset.
	check(len(data) >= 8, "TIFF header is shorter than eight bytes", kind=Fault.LAYOUT)
	if not header_ok:
		return _UNUSABLE
	order = "<" if data[:2] == b"II" else ">"
	def u16(offset):
		return struct.unpack_from(order + "H", data, offset)[0]
	def u32(offset):
		return struct.unpack_from(order + "I", data, offset)[0]
	first = u32(4)
	if not check(first >= 8, "missing first IFD", kind=Fault.VALUE):
		return _UNUSABLE
	pending, visited, active = [(first, False)], set(), set()
	widths = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
	while pending:
		offset, leaving = pending.pop()
		if leaving:
			active.remove(offset)
			visited.add(offset)
			continue
		if not check(offset not in active, "TIFF directory cycle", kind=Fault.VALUE):
			continue
		if offset in visited:
			continue
		active.add(offset)
		pending.append((offset, True))
		if not check(offset >= 8 and offset % 2 == 0 and offset + 2 <= len(data), f"IFD offset {offset} is invalid", kind=Fault.VALUE):
			continue
		count = u16(offset)
		# TIFF 6.0 section 2: "each IFD must have at least one entry".
		check(count > 0, "IFD has no entries", kind=Fault.VALUE)
		end = offset + 2 + 12 * count
		check(end + 4 <= len(data), "IFD entries or next-IFD pointer exceed profile bounds", kind=Fault.LAYOUT)
		previous_tag = -1
		for i in range(count):
			entry = offset + 2 + 12 * i
			tag, kind, n = u16(entry), u16(entry + 2), u32(entry + 4)
			# TIFF 6.0 section 2: entries are "sorted in ascending order" by Tag.
			check(tag > previous_tag, "IFD tags are not unique and ascending", kind=Fault.VALUE)
			previous_tag = tag
			if not check(kind in widths, f"TIFF field type {kind} has no defined width", kind=Fault.VALUE):
				continue
			size = widths[kind] * n
			value = entry + 8 if size <= 4 else u32(entry + 8)
			in_bounds = check(value >= 8 and value + size <= len(data), f"TIFF tag {tag:#x} value exceeds profile bounds", kind=Fault.VALUE)
			# TIFF 6.0 section 2: values begin on a "word boundary".
			aligned = check(value % 2 == 0, "TIFF value offset is not word aligned", kind=Fault.VALUE)
			if not in_bounds or not aligned:
				continue
			if tag in (0x8769, 0x8825, 0xa005, 0x014a) or kind == 13:
				if not check(kind in (4, 13), "IFD pointer is not LONG/IFD", kind=Fault.VALUE):
					continue
				if tag != 0x014a and kind != 13:
					if not check(n == 1, "Exif directory pointer must have one value", kind=Fault.VALUE):
						continue
				pending.extend((u32(value + 4 * j), False) for j in range(n))
		next_ifd = u32(end)
		if next_ifd:
			pending.append((next_ifd, False))
	return Values({"directories": len(visited)})
