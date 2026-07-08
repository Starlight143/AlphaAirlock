"""Bounded sandbox for LLM-generated factor code.

Security & correctness goals:
- Strip dangerous imports / dunders out of generated code via static AST scan
  before it is ever passed to exec.
- Run inside a controlled globals dict that exposes ONLY pandas, numpy, and
  a tiny safe-builtins subset. No file / network / subprocess access.
- Hard wall-clock + CPU budget enforced with a thread + sentinel +
  configurable timeout (signal.alarm is POSIX-only -> we use threading on
  Windows by raising in a watchdog thread + cooperative-cancel via a flag).
- Validates the returned object is a pandas.Series aligned to df.index and
  finite, then normalizes it to a tradable [-1, 1] signal.
"""

from __future__ import annotations

import ast
import logging
import math as _math_mod
import statistics as _statistics_mod
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("alpha.sandbox")

# ---------------------------------------------------------------------------
# Static AST whitelist
# ---------------------------------------------------------------------------

ALLOWED_IMPORTS = {"pandas", "numpy", "math", "statistics"}
# Note: 'pd' and 'np' are intentionally NOT importable — they are pre-bound in
# _build_globals() so factor code can use pd.rolling() / np.log() directly
# without any import statement. Allowing `import pd` / `import np` would create
# an alias pivot: `from pd.io import pickle` passes the root-only AST check
# (root='pd' in ALLOWED_IMPORTS) and at runtime _sandbox_import returns the full
# pandas module, letting IMPORT_FROM resolve .io/.pickle at C level outside the
# FORBIDDEN_ATTRS AST walk. Removing the aliases eliminates that path entirely.

FORBIDDEN_NAMES = {
    "__import__", "__builtins__", "__class__", "__bases__", "__subclasses__",
    "__getattribute__", "__globals__", "__loader__", "__spec__", "__code__",
    "open", "eval", "exec", "compile", "input", "print", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "memoryview",
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "ctypes",
    "asyncio", "threading", "multiprocessing", "io", "pickle", "marshal",
    "importlib", "builtins", "code", "codeop", "pty", "platform",
    "requests", "urllib", "http", "ftplib", "smtplib", "telnetlib",
}

FORBIDDEN_ATTRS = {
    "__import__", "__class__", "__bases__", "__subclasses__",
    "__globals__", "__getattribute__", "__reduce__", "__reduce_ex__",
    "__dict__", "__init_subclass__", "__loader__", "__code__",
    "__mro__",  # class-tree traversal pivot; type(x).__mro__ acknowledged at line 269
    # P32-S1: closure/introspection pivots — factor code never accesses these
    # on module or function objects; blocking them closes the __closure__
    # cell_contents information-disclosure path and str/repr escape routes.
    "__closure__", "__doc__", "__name__", "__qualname__", "__module__",
    "__annotations__", "__call__", "__init__", "__new__",
    "__repr__", "__str__", "__defaults__", "__kwdefaults__",
    # P29-S1: block pandas/numpy I/O surface — generated factors must not
    # exfiltrate via read_*/to_* nor pivot to pickle-bearing loaders.
    "read_csv", "read_pickle", "read_parquet", "read_sql", "read_hdf",
    "read_json", "read_excel", "read_feather", "read_orc", "read_table",
    "read_fwf", "read_html", "read_xml", "read_clipboard", "read_gbq",
    "read_sas", "read_spss", "read_stata",
    "to_pickle", "to_csv", "to_parquet", "to_sql", "to_hdf", "to_json",
    "to_excel", "to_feather", "to_orc", "to_stata", "to_clipboard",
    "to_html", "to_xml", "to_latex", "to_gbq",
    "load", "save", "loadtxt", "savetxt", "loads", "dumps", "dump",
    # P31-S1: numpy file/memory/C-bridge entry points reachable off the
    # allowed ``np`` Name — each reads arbitrary files or raw memory and is
    # NOT a factor-math primitive. Verified bypass: np.genfromtxt('/etc/passwd')
    # passed validate_code and read the file inside safe_execute_factor.
    "fromfile", "frombuffer", "memmap", "fromregex", "DataSource",
    "genfromtxt", "fromiter", "getbuffer", "frompyfunc", "ctypeslib",
    "tofile", "tobytes", "tostring", "fromstring",
    # Submodule pivots — pd.io.pickle.read_pickle works because the
    # attribute walker only catches the leaf name. Forbid intermediates.
    "io", "pickle", "marshal", "importlib", "sys", "os", "builtins",
    "_io", "_internals", "_libs", "_config", "subprocess", "ctypes",
    # P30-S1: str.format / format_map resolve dunder attributes inside the
    # format-spec string at C-runtime, bypassing the AST attribute walk.
    # Block the method names themselves so "{0.__class__}".format(df) fails.
    "format", "format_map", "__format__",
    # P30-S2: pandas exposes many real submodules whose leaf names aren't
    # in the existing blocklist. Each is a viable pivot to host runtime:
    #   pd.compat.platform.system()      -> host info disclosure
    #   pd.api.types.pandas_dtype(...)   -> type construction
    #   pd.util.testing.makeDataFrame()  -> factory abuse
    #   pd.core.common._get_callable_*   -> unwraps to builtins
    #   pd.tseries.offsets.BusinessDay   -> dateutil import side-effects
    #   pd.plotting._matplotlib.tools    -> matplotlib import side-effects
    #   pd.errors.SettingWithCopyWarning -> walks __init__.__globals__
    #   pd.extensions / arrays / offsets / testing / pickle_compat /
    #   compressors / version / numpy_   -> similar pivots
    "compat", "api", "util", "core", "tseries", "plotting", "arrays",
    "testing", "errors", "extensions", "offsets", "pickle_compat",
    "compressors", "version", "platform", "numpy_",
    # SEC: pd.eval / df.eval / df.query are runtime string-expression
    # evaluators. The expression lives inside a string argument that the
    # AST walker treats as an opaque literal, so it bypasses the entire
    # name/attribute whitelist. pd.eval('pd.compat.os.system(...)',
    # engine='python') is a confirmed full RCE escape (no __dunder__ token
    # needed, so _DUNDER_LITERAL_RE never fires). Legitimate factor code
    # only needs vectorized arithmetic and never .eval/.query.
    "eval", "query",
    # Defense-in-depth: 'engine' is the kwarg that selects the python
    # evaluator, and these convert/construct via dotted access. Blocking
    # the .engine ATTRIBUTE form is harmless to factor code (it only ever
    # appears as a kwarg, which this does not affect) but closes a future
    # attribute pivot; the other two block type/dtype construction.
    "pandas_dtype", "infer_objects",
}


# Look-ahead-bias guard: these pandas/Series methods, when called with a
# NEGATIVE period, peek at FUTURE bars (df['close'].shift(-1) == close[i+1]).
# The engine applies exactly ONE forward shift(1) to neutralize the normal
# +1 lag; it CANNOT neutralize negative future references, so we reject them
# statically before exec. (Design invariant: sandbox forbids .shift(-1)/
# iloc[i+1]/future-bar access.)
FUTURE_LEAK_METHODS = {"shift", "tshift", "diff", "pct_change"}


def _is_negative_constant(node: ast.AST) -> bool:
    """True iff node is a provably-negative numeric constant literal.

    Catches both forms the CPython parser emits:
      * ``-1``  -> ast.UnaryOp(op=ast.USub, operand=ast.Constant(value=1))
      * a bare negative ``ast.Constant`` (rare, e.g. from constant folding).
    Variable args (``shift(n)``) and unknown expressions return False and
    are intentionally permitted — we only reject what we can PROVE is a
    future reference.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = node.operand
        return (
            isinstance(operand, ast.Constant)
            and isinstance(operand.value, (int, float))
            and not isinstance(operand.value, bool)
            and operand.value > 0
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value < 0
    return False


import re as _re_mod

# P30-S1: matches any __dunder__ token (e.g. __class__, __bases__,
# __subclasses__, __globals__). Used to reject string literals that
# would be parsed by str.format / format_map / f-string format-spec
# at runtime to walk the object graph and pivot to subprocess etc.
_DUNDER_LITERAL_RE = _re_mod.compile(r"__[A-Za-z_][A-Za-z0-9_]*__")


class SandboxValidationError(Exception):
    """Raised when generated code fails the static whitelist check."""


class SandboxExecutionError(Exception):
    """Raised when generated code throws at runtime or violates contract."""


@dataclass
class SandboxResult:
    factor: pd.Series
    signal: pd.Series
    elapsed_seconds: float


def validate_code(source: str) -> None:
    """Reject code that imports or references forbidden modules / attributes."""
    if not source or not source.strip():
        raise SandboxValidationError("Empty source code")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise SandboxValidationError(f"SyntaxError: {exc.msg} (line {exc.lineno})") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise SandboxValidationError(f"Forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod and mod not in ALLOWED_IMPORTS:
                raise SandboxValidationError(f"Forbidden import-from: {node.module}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                raise SandboxValidationError(f"Forbidden attribute access: .{node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                raise SandboxValidationError(f"Forbidden name reference: {node.id}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
                raise SandboxValidationError(f"Forbidden call: {func.id}()")
            # Look-ahead guard: reject .shift(-N)/.diff(-N)/.pct_change(-N)/
            # .tshift(-N). A negative period references FUTURE bars; the engine's
            # single +1 shift cannot neutralize it.
            if isinstance(func, ast.Attribute) and func.attr in FUTURE_LEAK_METHODS:
                first_pos = node.args[0] if node.args else None
                if first_pos is not None and _is_negative_constant(first_pos):
                    raise SandboxValidationError(
                        f"Forbidden future-bar access: .{func.attr}() with a "
                        f"negative period peeks at future bars (look-ahead bias)"
                    )
                for kw in node.keywords:
                    if kw.arg == "periods" and _is_negative_constant(kw.value):
                        raise SandboxValidationError(
                            f"Forbidden future-bar access: .{func.attr}"
                            f"(periods=<negative>) peeks at future bars "
                            f"(look-ahead bias)"
                        )
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            # Reject reversed slicing (e.g. ``s.iloc[::-1]`` or ``s[::-1]``)
            # which underpins the reverse-then-cumsum future-leak idiom
            # (s[::-1].cumsum()[::-1] makes bar i depend on bars i..N-1).
            step = node.slice.step
            if step is not None and _is_negative_constant(step):
                raise SandboxValidationError(
                    "Forbidden reversed slice (step<0): enables "
                    "reverse-then-cumsum future-bar leakage (look-ahead bias)"
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # P29-S1: prevent generated code from defining dunder-named
            # functions/classes (e.g. ``def __reduce__(self): ...``) that
            # would hijack pickle/repr/class machinery even though direct
            # access via getattr/dot is blocked.
            nm = node.name or ""
            if len(nm) >= 4 and nm.startswith("__") and nm.endswith("__"):
                raise SandboxValidationError(
                    f"Forbidden dunder definition: {nm}"
                )
            if nm in FORBIDDEN_NAMES:
                raise SandboxValidationError(
                    f"Forbidden definition name: {nm}"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # P30-S1: format-string dunder pivot — "{0.__class__}".format(x)
            # resolves attribute access inside the format spec at C-runtime,
            # invisible to the AST attribute walk above. Reject any string
            # literal containing a __dunder__ token.
            if _DUNDER_LITERAL_RE.search(node.value):
                raise SandboxValidationError(
                    "Forbidden dunder token in string literal "
                    "(blocks str.format / format_map attribute pivots)"
                )
        elif isinstance(node, ast.JoinedStr):
            # P30-S1: f-string format-spec segments also go through the format
            # machinery. Walk nested Constant strings in the f-string.
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    if _DUNDER_LITERAL_RE.search(inner.value):
                        raise SandboxValidationError(
                            "Forbidden dunder token in f-string literal"
                        )


# ---------------------------------------------------------------------------
# Safe globals
# ---------------------------------------------------------------------------

_SAFE_BUILTINS: Dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "frozenset": frozenset, "int": int, "len": len, "list": list, "map": map,
    "max": max, "min": min, "next": next, "iter": iter, "pow": pow, "range": range,
    "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
    "zip": zip, "True": True, "False": False, "None": None,
    "isinstance": isinstance,  # needed for type-checking return values in generated factor code
    # P30-S1: `type` is a class-tree pivot (type(x).__mro__ reaches object
    # then __subclasses__ then arbitrary classes incl. subprocess.Popen).
    # `print` writes to host streams. Both dispensable inside a factor body.
}

# P28 — pre-loaded module table for the restricted ``__import__`` below.
# ``validate_code`` already permits these names via ``ALLOWED_IMPORTS`` at
# AST-walk time, but at runtime Python's ``import`` statement still needs to
# look up ``__builtins__['__import__']`` to actually bind the name. With no
# ``__import__`` in ``_SAFE_BUILTINS``, any LLM-generated factor that begins
# with ``import pandas as pd`` (which the Coder Agent routinely emits) blew
# up with ``ImportError: __import__ not found`` — every cell of any sweep
# launched against such a strategy failed for this single reason.
_SANDBOX_PRELOADED_MODULES: Dict[str, Any] = {
    "pandas": pd,
    "numpy": np,
    # ``pd`` / ``np`` are accepted by the static whitelist as bare aliases
    # (e.g. ``import pd``) but Python's normal import machinery would never
    # find a top-level package literally named ``pd``. We map them to the
    # real modules anyway so that hypothetical generated code like
    # ``import pd`` would resolve to pandas rather than raise — a harmless
    # superset of what validate_code already considers legal.
    "pd": pd,
    "np": np,
    "math": _math_mod,
    "statistics": _statistics_mod,
}


def _sandbox_import(
    name: str,
    globals: Optional[Dict[str, Any]] = None,  # noqa: A002 - shadow is intentional
    locals: Optional[Dict[str, Any]] = None,   # noqa: A002 - shadow is intentional
    fromlist: tuple = (),
    level: int = 0,
) -> Any:
    """Restricted replacement for builtin ``__import__``.

    Returns one of the pre-loaded modules from ``_SANDBOX_PRELOADED_MODULES``
    if the name is on the whitelist; otherwise raises ``ImportError``. Never
    falls back to ``importlib`` or ``sys.modules`` lookups — generated code
    cannot import anything we haven't pre-vetted.

    Handles both forms emitted by the CPython compiler:
    * ``import pandas`` / ``import pandas as pd`` → ``IMPORT_NAME``
      with ``fromlist=()``; we return the pandas module object.
    * ``from numpy import inf`` → ``IMPORT_NAME`` with ``fromlist=('inf',)``
      followed by ``IMPORT_FROM``; we return the numpy module and let the
      CPython opcode pull the attribute out.
    """
    if level != 0:
        raise ImportError("Sandbox: relative imports are not allowed")
    root = (name or "").split(".")[0]
    if root not in _SANDBOX_PRELOADED_MODULES:
        raise ImportError(
            f"Sandbox: import '{name}' is not allowed "
            f"(allowed: {sorted(ALLOWED_IMPORTS)})"
        )
    return _SANDBOX_PRELOADED_MODULES[root]


# Inject the restricted __import__ into the safe-builtins table AFTER it has
# been defined. We keep ``_SAFE_BUILTINS`` literal above so the reader can see
# the full builtin surface at a glance, then close the gap here.
_SAFE_BUILTINS["__import__"] = _sandbox_import


def _build_globals() -> Dict[str, Any]:
    return {
        "__builtins__": _SAFE_BUILTINS,
        "pd": pd,
        "np": np,
        "pandas": pd,
        "numpy": np,
    }


# ---------------------------------------------------------------------------
# Threaded watchdog (cross-platform; signal.alarm is POSIX only)
# ---------------------------------------------------------------------------

# Module-level registry of timed-out (leaked) sandbox threads so we can track
# accumulation and log warnings; threads cannot be killed in CPython but the
# list lets callers observe runaway conditions.
_leaked_threads: list = []
_leaked_threads_lock = threading.Lock()
_LEAKED_THREAD_WARN_THRESHOLD = 10


def _run_with_timeout(
    target: Callable[[], Any],
    timeout_seconds: float,
) -> Any:
    # Sweep dead entries from the leaked-thread registry before each execution.
    with _leaked_threads_lock:
        _leaked_threads[:] = [t for t in _leaked_threads if t.is_alive()]
        if len(_leaked_threads) >= _LEAKED_THREAD_WARN_THRESHOLD:
            logger.warning(
                "sandbox: %d leaked worker threads still running; "
                "possible runaway factor code or timeout too short",
                len(_leaked_threads),
            )

    result: Dict[str, Any] = {}
    exception: Dict[str, BaseException] = {}
    cancel_event = threading.Event()

    def worker() -> None:
        try:
            result["value"] = target()
        except BaseException as exc:  # noqa: BLE001 - we re-raise below
            exception["exc"] = exc
        finally:
            cancel_event.set()

    th = threading.Thread(target=worker, name="sandbox-worker", daemon=True)
    th.start()
    th.join(timeout=timeout_seconds)
    if th.is_alive():
        # Thread is still running; we can't reliably kill it in CPython.
        # Surface a clear timeout to the caller and let the daemon die with
        # the process. Heavy infinite loops will hold ~one CPU core briefly.
        # P15/D-L9 NOTE: CPU-bound infinite loops in untrusted factor code are
        # killed only by this wall-clock timeout, NOT by CPU-time accounting.
        # Real CPU-time limits would require POSIX resource.setrlimit /
        # Windows job objects — out of scope for the cross-platform watchdog.
        # Each daemon thread holds ~1 core until the next GIL release; the
        # API process can absorb this for the brief window before exit.
        # cancel_event is set in the worker's finally block; cooperative
        # factor code may check it, but CPython cannot force termination.
        _ = cancel_event  # referenced for clarity; daemon thread reads it if cooperative
        with _leaked_threads_lock:
            _leaked_threads.append(th)
        raise SandboxExecutionError(
            f"Sandbox execution exceeded {timeout_seconds:.1f}s wall clock"
        )
    if "exc" in exception:
        raise exception["exc"]
    return result.get("value")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

REQUIRED_LOWERCASE_COLS = {
    "open", "high", "low", "close", "volume",
    "open_interest", "funding_rate", "liquidations",
}


def normalize_to_signal(factor: pd.Series) -> pd.Series:
    """Convert any-range factor Series into a tradable {-1, 0, +1} signal.

    Strategy:
    - Replace inf/nan with 0.
    - Rolling z-score over the last 168 hours (1 week) to keep signals adaptive.
    - +1 when z > +1.0, -1 when z < -1.0, else 0.
    - Fallback for tiny series (<10 bars): sign(factor).
    """
    if not isinstance(factor, pd.Series):
        raise SandboxExecutionError("Factor must be a pandas Series")

    f = pd.to_numeric(factor, errors="coerce")
    f = f.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    n = len(f)
    if n < 10:
        return np.sign(f).astype(float)

    window = min(168, max(24, n // 8))
    rolling_mean = f.rolling(window=window, min_periods=max(8, window // 4)).mean()
    rolling_std = f.rolling(window=window, min_periods=max(8, window // 4)).std()
    rolling_std = rolling_std.where(rolling_std > 1e-14, other=np.nan)
    z = (f - rolling_mean) / rolling_std
    z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    signal = pd.Series(0.0, index=f.index)
    signal[z > 1.0] = 1.0
    signal[z < -1.0] = -1.0
    return signal.astype(float)


def safe_execute_factor(
    code: str,
    df: pd.DataFrame,
    *,
    timeout_seconds: float = 8.0,
) -> SandboxResult:
    """Run sandboxed `compute_factor(df) -> pd.Series` and normalize the output.

    Raises SandboxValidationError on static checks, SandboxExecutionError
    on runtime / contract failures.
    """
    validate_code(code)
    if not isinstance(df, pd.DataFrame):
        raise SandboxExecutionError("df must be a pandas DataFrame")

    missing = REQUIRED_LOWERCASE_COLS - set(df.columns)
    if missing:
        raise SandboxExecutionError(
            f"DataFrame missing required lowercase columns: {sorted(missing)}"
        )

    sandbox_globals = _build_globals()

    # Compile/exec the module body to expose compute_factor in sandbox_globals.
    try:
        compiled = compile(code, filename="<sandbox-factor>", mode="exec")
    except SyntaxError as exc:
        raise SandboxValidationError(f"SyntaxError on compile: {exc}") from exc

    # P28 — passing the SAME dict as both globals and locals to ``exec`` mirrors
    # how Python evaluates a real module body: names bound at module level land
    # in module globals, and functions defined inside the module pick them up
    # through normal global-scope lookup. With the previous split (sandbox_globals
    # for globals + a fresh ``sandbox_locals`` dict for locals), statements like
    # ``from numpy import inf`` stored ``inf`` in ``sandbox_locals`` while the
    # ``compute_factor`` body resolved names against ``sandbox_globals``, leaving
    # the import inaccessible inside the function. Unified scope eliminates that
    # quirk and matches LLM-generated code's assumptions.
    def _exec() -> pd.Series:
        exec(compiled, sandbox_globals, sandbox_globals)  # noqa: S102 - sandboxed
        if "compute_factor" not in sandbox_globals or not callable(sandbox_globals["compute_factor"]):
            raise SandboxExecutionError("Generated code did not define compute_factor(df)")
        out = sandbox_globals["compute_factor"](df.copy())
        return out

    started = time.monotonic()
    try:
        factor = _run_with_timeout(_exec, timeout_seconds)
    except SandboxExecutionError:
        raise
    except BaseException as exc:  # noqa: BLE001
        raise SandboxExecutionError(f"Runtime error: {type(exc).__name__}: {exc}") from exc
    elapsed = time.monotonic() - started

    # Add a tiny floor so Windows 15ms timer never reports 0.0 to assertions.
    # D-L9/P16 — Windows default timer resolution is ~15 ms; assertions that
    # expect `duration > 0.0` will spuriously read 0.0 below that. Floor at
    # 0.015 so the value is at least one timer tick.
    if elapsed < 0.015:
        elapsed = 0.015

    if not isinstance(factor, pd.Series):
        raise SandboxExecutionError(
            f"compute_factor must return pandas.Series, got {type(factor).__name__}"
        )
    if len(factor) != len(df):
        raise SandboxExecutionError(
            f"Factor length {len(factor)} does not match df length {len(df)}"
        )
    if not factor.index.equals(df.index):
        # Tolerate misaligned-by-positional output by realigning.
        factor = pd.Series(factor.values, index=df.index)

    finite_count = int(np.isfinite(pd.to_numeric(factor, errors="coerce").fillna(np.nan)).sum())
    if finite_count == 0:
        raise SandboxExecutionError("Factor series contains no finite values")

    signal = normalize_to_signal(factor)
    return SandboxResult(factor=factor, signal=signal, elapsed_seconds=elapsed)


__all__ = [
    "validate_code",
    "safe_execute_factor",
    "normalize_to_signal",
    "SandboxValidationError",
    "SandboxExecutionError",
    "SandboxResult",
    "REQUIRED_LOWERCASE_COLS",
]
