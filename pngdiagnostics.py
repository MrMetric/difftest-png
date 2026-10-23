"""One append-only record for PNG and inflate coverage and faults.

Coverage is a projection of events, never independently writable. A fault may carry a
cause (for example bad DEFLATE inside ancillary text); the cause is printed with it.
"""
import csv
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


if not __debug__:
	raise RuntimeError("the reference decoder requires Python assertions (do not use -O)")

with (Path(__file__).resolve().parent / "spec/clauses.tsv").open(newline="") as f:
	_rows = list(csv.DictReader(f, delimiter="\t"))
assert len({r["id"] for r in _rows}) == len(_rows), "duplicate clause ids"
CONSEQUENCES = MappingProxyType({r["id"]: r["consequence"] for r in _rows})


@dataclass(frozen=True)
class Event:
	id: str
	level: str
	message: str = ""
	cause: str | None = None


class Diagnostics:
	def __init__(self):
		self.__events: list[Event] = []
		self.__seen: set[Event] = set()
		self.__fault_count = 0

	def record(self, id: str, level: str = "INFO", message: str = "", cause: str | None = None) -> Event:
		assert id in CONSEQUENCES, f"unknown clause {id}"
		assert level in ("INFO", "WARN", "ERR"), level
		if level == "INFO":
			assert CONSEQUENCES[id] == "none", f"fault {id} requires a diagnostic"
			assert not message and cause is None, "coverage cannot carry a fault"
		else:
			allowed = {"ERR": {"reject", "either"}, "WARN": {"recover", "either", "still"}}
			assert CONSEQUENCES[id] in allowed[level], f"{level} incompatible with {id}: {CONSEQUENCES[id]}"
			assert message and "\n" not in message and "\r" not in message, "diagnostics occupy one stdout line"
			if cause is not None:
				assert cause in CONSEQUENCES and CONSEQUENCES[cause] != "none", cause
		event = Event(id, level, message, cause)
		if level != "INFO":
			# Occurrences determine field usability even when an identical diagnostic is already present.
			self.__fault_count += 1
		# Coverage and repeated LZ77 conditions need only one event per distinct fact.
		if event not in self.__seen:
			self.__events.append(event)
			self.__seen.add(event)
		return event

	@property
	def fault_count(self) -> int:
		return self.__fault_count

	@property
	def events(self) -> tuple[Event, ...]:
		return tuple(self.__events)

	@property
	def reached(self) -> frozenset[str]:
		return frozenset(id for e in self.__events for id in (e.id, e.cause) if id is not None)

	@property
	def warnings(self) -> list[tuple[str, str]]:
		return [(e.id, self.message(e)) for e in self.__events if e.level == "WARN"]

	@staticmethod
	def message(e: Event) -> str:
		return (f"[{e.cause}] " if e.cause else "") + e.message

	def emit(self) -> None:
		print("INFO reached=" + ",".join(sorted(self.reached)))
		for e in self.__events:
			if e.level != "INFO":
				print(f"{e.level} [{e.id}] {self.message(e)}")


class StreamDiagnostics:
	"""A stream's error policy, applied at the inflate boundary before control returns.

	Ancillary failures have a recoverable PNG clause with the RFC cause attached.
	An image caller supplies its completion policy. Raw streams default to fatal failures.
	"""
	def __init__(self, record: Diagnostics, name: str, ancillary: str | None = None, *, failure=None):
		assert ancillary is None or failure is None, "a stream has one failure policy"
		self.record, self.name, self.ancillary, self.on_failure = record, name, ancillary, failure

	def reach(self, id: str) -> None:
		self.record.record(id)

	def fault(self, id: str, message: str) -> None:
		self.record.record(id, "WARN", f"{self.name}: {message}")

	def failure(self, error) -> None:
		id, message = error.id, str(error)
		if self.ancillary:
			self.record.record(self.ancillary, "WARN", f"{self.name}: {message}; ignored", cause=id)
		elif self.on_failure is not None:
			start = self.record.fault_count
			self.on_failure(error)
			assert self.record.fault_count > start, "stream failure policy must record its diagnostic"
		else:
			self.record.record(id, "ERR", f"{self.name}: {message}")
