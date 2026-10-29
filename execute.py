"""Run one implementation (a column) on one file, and keep the result.

Every run is stored in out/runs.sqlite under its column (name, command line, environment, limits, and its
identity: the program that produced the result) and the file's path and contents. A stored result is read back
only when the caller asks for reuse, which run.py's --reuse does to save time while iterating, and only for
the same program: identity() covers the version string the column reports, its `revision`, the content of every
file and directory its definition names, and the shared objects its executables load. The reference columns
are always run afresh, since they are what changes during development.

Columns see only BASE_ENV plus their own `env`, never the launching shell's environment, so a variable such as
LD_LIBRARY_PATH or _JAVA_OPTIONS cannot change what they do.

A column whose program is not installed is its own case, not a verdict: run_one raises Missing when the
program cannot be executed, or when it exits 127, the status the shell and this project's shims use for a
command or decoder library that is not there.
"""
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from compression import zstd
from pathlib import Path

import numpy as np

import pam

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "out" / "runs.sqlite"
# valgrind finds glibc's debug information in the installed glibc-debug package, never over the network:
# DEBUGINFOD_URLS is deliberately absent.
BASE_ENV = {"PATH": "/usr/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"}
MEM_PAT = re.compile(r"out\s*of\s*memory|outofmem\b|MemoryError|bad_alloc|cannot allocate|allocation .*failed|memory allocation|alloc(ation)? of \d+ bytes", re.I)
FSIZE_LIMIT = 1 << 30
CAPTURE_LIMIT = 16 * 1024
VALGRIND_ERR = 99
NOT_INSTALLED = 127
SHIM_FAILURE = 70  # the adapter itself failed: not scored, a problem to fix
API_FAILURE = 71  # the library returned without error, but not an image: its result, a must-violation
# About five times the slowest memcheck run that completes on the suite (25 s); a run that needs longer is hung.
MEMCHECK_TIMEOUT_S = 120.0


class Missing(Exception):
	"""The column's program, or the decoder library its shim loads, is not installed."""


def child_env(impl) -> dict[str, str]:
	return BASE_ENV | impl.env


def _limits(impl, memcheck: bool) -> tuple[float, int]:
	"""(timeout in seconds, address-space limit in bytes); valgrind needs room for its shadow memory."""
	return MEMCHECK_TIMEOUT_S if memcheck else impl.timeout_s, (impl.mem_mb * (4 if memcheck else 1)) << 20


_con: sqlite3.Connection | None = None


def _store() -> sqlite3.Connection:
	global _con
	if _con is None:
		STORE.parent.mkdir(parents=True, exist_ok=True)
		_con = sqlite3.connect(STORE, timeout=120)
		_con.execute("PRAGMA busy_timeout = 120000")
		_con.execute("PRAGMA journal_mode = WAL")
		_con.executescript("""
			CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, column_name TEXT NOT NULL, path TEXT NOT NULL,
				memcheck INTEGER NOT NULL, result TEXT NOT NULL);
			CREATE TABLE IF NOT EXISTS images (digest TEXT PRIMARY KEY, shape TEXT NOT NULL, zstd BLOB NOT NULL);
		""")
	return _con


_hashes: dict[str, str] = {}
_ldd: dict[tuple[str, str], dict[str, str]] = {}
SHELLS = ("sh", "bash")


def _hash(path: Path) -> str:
	"""sha256 of a file's content, or of a directory: the relative path and content of every regular file under it,
	in sorted order. Bytecode in __pycache__ is left out: an interpreter rewrites it from the sources beside it."""
	real = os.path.realpath(path)
	if real not in _hashes:
		if os.path.isdir(real):
			h = hashlib.sha256()
			for p in sorted(Path(real).rglob("*")):
				rel = p.relative_to(real)
				if p.is_file() and "__pycache__" not in rel.parts:
					h.update(f"{rel}\0{_hash(p)}\n".encode())
			_hashes[real] = h.hexdigest()
		else:
			with open(real, "rb") as f:
				_hashes[real] = hashlib.file_digest(f, "sha256").hexdigest()
	return _hashes[real]


def _libraries(program: str, env: dict[str, str]) -> dict[str, str]:
	"""The shared objects ldd resolves for an ELF executable under the column's environment -> their content hash."""
	key = (os.path.realpath(program), json.dumps(env, sort_keys=True))
	if key not in _ldd:
		r = subprocess.run(["ldd", program], capture_output=True, text=True, env=env, timeout=60)
		libs: dict[str, str] = {}
		if r.returncode:
			assert "not a dynamic executable" in r.stdout + r.stderr, f"ldd {program}: exit {r.returncode}\n{r.stdout}{r.stderr}"
		for line in r.stdout.splitlines():
			target = line.split("=>")[-1].split(" (")[0].strip()
			if target.startswith("/"):
				libs[target] = _hash(Path(target))
			elif target == "not found":
				libs[line.split("=>")[0].strip()] = target
		_ldd[key] = libs
	return _ldd[key]


def identity(impl, version: str | None) -> str:
	"""The program a column runs, as a digest of, in order: the version string it reports (`version`), its
	`revision`, the content of every path its definition names that exists, and the shared objects its ELF
	executables load.

	Named paths: each whitespace-separated word of a `cmd` argument that is an absolute path, with the regular
	files beside it when it is a file (what a script imports from its own directory, what a .NET apphost loads);
	the program, cmd[0], and the words of a `sh -c` script, resolved through the column's PATH; each absolute
	entry of an `env` value (PATH-style lists split on ':'); and `creates`. A file hashes by content, a directory
	by its files (_hash). Every named file that is an ELF executable contributes its ldd resolution, run under the
	column's environment so its LD_LIBRARY_PATH takes effect."""
	env = child_env(impl)
	words = [w.strip("'\"") for a in impl.cmd for w in a.split()]
	beside = [Path(w) for w in words if w.startswith("/")]
	programs = [impl.cmd[0]] + (impl.cmd[2].split() if Path(impl.cmd[0]).name in SHELLS and impl.cmd[1:2] == ["-c"] else [])
	on_path = [Path(found) for w in programs if "/" not in w and (found := shutil.which(w, path=env["PATH"]))]
	listed = [Path(part) for v in impl.env.values() for part in v.split(":") if part.startswith("/")]
	named = [p for p in beside + on_path + listed + ([Path(impl.creates)] if impl.creates else []) if p.exists()]
	paths = {str(p): _hash(p) for p in named}
	for p in beside:
		if p.is_file():
			paths |= {str(s): _hash(s) for s in p.parent.iterdir() if s.is_file()}
	libraries: dict[str, str] = {}
	for p in named:
		if p.is_file():
			with open(p, "rb") as f:
				if f.read(4) == b"\x7fELF":
					libraries |= _libraries(str(p), env)
	return hashlib.sha256(json.dumps([version, impl.revision, paths, libraries], sort_keys=True).encode()).hexdigest()


def _key(impl, file: Path, data: bytes, memcheck: bool) -> str:
	assert impl.identity, f"{impl.name}: the column's identity was not computed (execute.identity)"
	timeout, mem = _limits(impl, memcheck)
	column = {"name": impl.name, "cmd": impl.cmd, "env": child_env(impl), "timeout": timeout, "mem": mem,
		"pixels": impl.pixels, "memcheck": memcheck, "identity": impl.identity}
	return hashlib.sha256(json.dumps([column, str(file), hashlib.sha256(data).hexdigest()], sort_keys=True).encode()).hexdigest()


def run_one(impl, file: Path, memcheck: bool, reuse: bool = False) -> tuple[dict, bool]:
	"""(the column's result on the file, whether it is a stored one): a fresh run, which is stored, or with `reuse`
	the stored one if there is one (never for the reference columns). A timeout is never stored, since it depends
	on the machine's load."""
	file = file.resolve()
	key = _key(impl, file, file.read_bytes(), memcheck)
	con = _store()
	if reuse and impl.project != "reference":
		row = con.execute("SELECT result FROM results WHERE key = ?", (key,)).fetchone()
		if row:
			r = json.loads(row[0])
			if r.get("image"):
				shape, z = con.execute("SELECT shape, zstd FROM images WHERE digest = ?", (r["image"],)).fetchone()
				r["image"] = (r["image"], tuple(json.loads(shape)), z)
			return r, True
	r, timed_out = _execute(impl, file, memcheck)
	if not timed_out:
		stored = dict(r)
		with con:
			if r.get("image"):
				digest, shape, z = r["image"]
				con.execute("INSERT OR IGNORE INTO images VALUES (?, ?, ?)", (digest, json.dumps(shape), z))
				stored["image"] = digest
			con.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?)", (key, impl.name, str(file), int(memcheck), json.dumps(stored)))
	return r, False


def run_limited(argv: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float, mem_bytes: int) -> tuple[int | None, int | None, bool]:
	"""Run argv in its own process group; return (exit code, terminating signal, timed out)."""
	def preexec():
		os.setsid()
		resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
		resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_LIMIT, FSIZE_LIMIT))
		resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
	with open(cwd / "stdout", "wb") as so, open(cwd / "stderr", "wb") as se:
		try:
			p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=so, stderr=se, preexec_fn=preexec, env=env)
		except (FileNotFoundError, PermissionError) as e:
			raise Missing(f"{argv[0]}: {e.strerror}") from None
		try:
			p.wait(timeout_s)
		except subprocess.TimeoutExpired:
			try:
				os.killpg(p.pid, signal.SIGKILL)
			except ProcessLookupError:
				pass
			p.wait()
			return None, None, True
	if p.returncode < 0:
		return None, -p.returncode, False
	return p.returncode, None, False


def diagnostics(path: Path, *, stderr: bool = False) -> dict:
	"""Scan output once, keeping a capped display prefix and bounded diagnostic messages.

	The display cap must not hide a later WARN, the actual ERR, or allocation failure.
	Messages themselves retain at most CAPTURE_LIMIT bytes from their line.
	"""
	result = {"text": "", "warning": None, "error": None, "last": None, "oom": False}
	if not path.exists():
		return result
	with path.open("rb") as f:
		capture, total = bytearray(), 0
		head, message, tail = b"", "", ""
		while part := f.readline(CAPTURE_LIMIT):
			capture.extend(part[:CAPTURE_LIMIT - len(capture)])
			total += len(part)
			if len(head) < CAPTURE_LIMIT:
				head += part[:CAPTURE_LIMIT - len(head)]
			text = part.decode("utf-8", "replace")
			# Keep a diagnostic even if indentation fills the entire display prefix.
			message = message or text.strip()
			result["oom"] |= bool(MEM_PAT.search(tail + text))
			tail = (tail + text)[-256:]
			if part.endswith(b"\n"):
				_record_line(result, head.decode("utf-8", "replace"), message, stderr)
				head, message = b"", ""
		if head:
			_record_line(result, head.decode("utf-8", "replace"), message, stderr)
	result["text"] = capture.decode("utf-8", "replace")
	if total > CAPTURE_LIMIT:
		result["text"] += f"\n[... {total - CAPTURE_LIMIT} more bytes]"
	return result


def _record_line(result: dict, line: str, message: str, stderr: bool) -> None:
	if message:
		result["last"] = message
		if result["warning"] is None:
			if stderr:
				result["warning"] = message
			elif line.startswith(("WARN ", "ERR ")):
				result["warning"] = line.split(" ", 1)[1].strip()
	if line.startswith(("ERR ", "SHIM ", "API ")):
		result["error"] = line.split(" ", 1)[1].strip()


def _memcheck(td: Path, code: int | None, timed_out: bool) -> dict:
	"""The first memory error in valgrind's XML reports (one per process), or a timeout.

	valgrind that cannot run to completion for any other reason exits like a decoder that rejects the file, so
	without its FINISHED status the absence of an error would mean nothing: that is an error of the harness."""
	logs = "".join(p.read_text("utf-8", "replace") for p in sorted(td.glob("valgrind.*")) if p.suffix != ".xml")
	if timed_out:
		return {"memory": None, "timeout": True, "log": logs[:CAPTURE_LIMIT]}
	reports = sorted(td.glob("valgrind.*.xml"))
	errors, finished = [], bool(reports)
	for p in reports:
		try:
			root = ET.parse(p).getroot()
		except ET.ParseError:
			finished = False
			continue
		finished &= "FINISHED" in [s.findtext("state") for s in root.iter("status")]
		# The XML reports leaks as errors too; the memory pass is about invalid accesses and uses, not leaks.
		errors += [e.findtext("what") or e.findtext("kind") for e in root.iter("error") if not e.findtext("kind", "").startswith("Leak_")]
	if not finished:
		raise RuntimeError(f"valgrind did not run to completion (exit {code}):\n{logs[:2000]}")
	assert code != VALGRIND_ERR or errors, f"valgrind exit {VALGRIND_ERR} without an error in its reports:\n{logs[:2000]}"
	return {"memory": errors[0] if errors else None, "timeout": False, "log": logs[:CAPTURE_LIMIT]}


def _execute(impl, file: Path, memcheck: bool) -> tuple[dict, bool]:
	"""One invocation: (the raw verdict, its message, stdout and the decoded RGBA16 pixels if any; timed out)."""
	(ROOT / "out" / "tmp").mkdir(parents=True, exist_ok=True)
	with tempfile.TemporaryDirectory(dir=ROOT / "out" / "tmp") as td_:
		td = Path(td_)
		out_path = td / "out.pam"
		argv = [a.replace("{input}", str(file)).replace("{output}", str(out_path)) for a in impl.cmd]
		if memcheck:
			argv = ["valgrind", "-q", "--trace-children=yes", f"--error-exitcode={VALGRIND_ERR}", "--log-file=" + str(td / "valgrind.%p"),
				"--errors-for-leak-kinds=none", "--xml=yes", "--xml-file=" + str(td / "valgrind.%p.xml")] + argv
		timeout, mem = _limits(impl, memcheck)
		t0 = time.monotonic()
		code, sig, timed_out = run_limited(argv, cwd=td, env=child_env(impl), timeout_s=timeout, mem_bytes=mem)
		seconds = round(time.monotonic() - t0, 3)  # wall time, measured on a machine running other columns too
		if memcheck and code != NOT_INSTALLED:
			return _memcheck(td, code, timed_out) | {"seconds": seconds}, timed_out
		so, se = diagnostics(td / "stdout"), diagnostics(td / "stderr", stderr=True)
		reason = so["error"] or se["last"] or so["last"] or f"exit {code}"
		if code == NOT_INSTALLED:
			raise Missing(reason)
		oom = so["oom"] or se["oom"]
		row = {"stdout": so["text"], "stderr": se["text"], "image": None, "maxval": None, "warning": so["warning"] or se["warning"], "seconds": seconds}
		if timed_out:
			return row | {"verdict": "timeout", "detail": f"killed after {impl.timeout_s:g} s"}, True
		if sig is not None:
			name = signal.Signals(sig).name if sig in signal.Signals._value2member_map_ else f"signal {sig}"
			if sig == signal.SIGXFSZ:
				return row | {"verdict": "shim", "detail": "output exceeded the size limit"}, False
			return row | {"verdict": "oom" if oom else "crash", "detail": f"{name}: {reason}"}, False
		if code == API_FAILURE:
			return row | {"verdict": "api", "detail": reason}, False
		if code == SHIM_FAILURE:
			return row | {"verdict": "shim", "detail": reason}, False
		if code != 0:
			return row | {"verdict": "oom" if oom else "reject", "detail": reason}, False
		if not impl.pixels:
			return row | {"verdict": "accept", "detail": row["warning"]}, False
		if not out_path.exists() or out_path.stat().st_size == 0:
			return row | {"verdict": "shim", "detail": "exit 0 but no image written"}, False
		try:
			p = pam.parse(out_path.read_bytes())
			return row | {"verdict": "accept", "detail": row["warning"], "image": pack(pam.rgba16(p)), "maxval": p.maxval}, False
		except Exception as e:  # malformed output is a finding about the shim
			return row | {"verdict": "shim", "detail": f"cannot read the output PAM: {type(e).__name__}: {e}"}, False


def pack(rgba: np.ndarray) -> tuple[str, tuple, bytes]:
	"""(digest, shape, zstd bytes): how decoded pixels travel between processes and sit in the store."""
	return pam.digest(rgba), rgba.shape, zstd.compress(rgba.tobytes(), 3)
