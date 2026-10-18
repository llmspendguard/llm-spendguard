"""Bridge pytest-fixture-style test files into the repo's subprocess script-runner.

The canonical suite entry is bare ``pytest``, which (per pyproject ``python_files=["test_runner.py"]``
and tests/conftest.py ``pytest_collectstart``) collects ONLY tests/test_runner.py. That runner then
subprocess-runs every other ``tests/test_*.py`` as plain ``python <script>`` in its own isolated
``SPENDGUARD_HOME`` and asserts the process exits 0 with no ``[FAIL]``/``FAIL:`` in stdout.

Some files are written pytest-native — ``def test_x(monkeypatch, tmp_path, ...)`` with bare ``assert``
and no ``__main__`` — so run as a plain script they DEFINE the functions and exit 0 with ZERO assertions
executed (silent false-greens). ``run_pytest_style(ns)`` closes that gap WITHOUT changing the isolation
model: a one-line ``__main__`` in each such file calls it, and it discovers the ``test_*`` functions,
supplies REAL fixtures (``monkeypatch`` via :class:`_pytest.monkeypatch.MonkeyPatch`, ``tmp_path`` via a
fresh temp dir), expands ``@pytest.mark.parametrize``, runs every assertion, prints ``[OK]``/``[FAIL]``,
and returns a nonzero exit code on any failure. An UNKNOWN fixture is a loud failure, never a silent skip.
"""
import inspect
import itertools
import pathlib
import tempfile
import traceback

from _pytest.monkeypatch import MonkeyPatch


def _parametrizations(fn):
    """Yield one ``{argname: value}`` dict per parametrized case (one empty dict when not parametrized)."""
    marks = [m for m in (getattr(fn, "pytestmark", []) or [])
             if getattr(m, "name", None) == "parametrize"]
    if not marks:
        return [{}]

    per_mark = []
    for m in marks:
        argnames, argvalues = m.args[0], m.args[1]
        if isinstance(argnames, str):
            names = [a.strip() for a in argnames.split(",") if a.strip()]
        else:
            names = list(argnames)
        cases = []
        for entry in argvalues:
            values = getattr(entry, "values", None)  # unwrap pytest.param(...) (ParameterSet)
            if values is None:
                values = entry if len(names) > 1 else (entry,)
            cases.append(dict(zip(names, values)))
        per_mark.append(cases)

    combined = []
    for combo in itertools.product(*per_mark):
        merged = {}
        for d in combo:
            merged.update(d)
        combined.append(merged)
    return combined


def _call_kwargs(fn, bound, teardowns, unknown_sink):
    """Build kwargs for ``fn``; inject monkeypatch/tmp_path. Record an unknown fixture and return None."""
    kwargs = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in bound:
            kwargs[name] = bound[name]
        elif name == "monkeypatch":
            mp = MonkeyPatch()
            teardowns.append(mp.undo)
            kwargs[name] = mp
        elif name == "tmp_path":
            kwargs[name] = pathlib.Path(tempfile.mkdtemp(prefix="sg-bridge-"))
        elif param.default is not inspect.Parameter.empty:
            continue  # not injected, not parametrized, but has a default — let the default stand
        else:
            unknown_sink.append(name)
            return None
    return kwargs


def run_pytest_style(ns):
    """Run every ``test_*`` function in namespace ``ns`` with real fixtures. Return 0 if all pass else 1."""
    module = ns.get("__name__") or "tests"
    tests = sorted((name, obj) for name, obj in ns.items()
                   if name.startswith("test_") and inspect.isfunction(obj))

    ran = 0
    failures = 0
    for name, fn in tests:
        for bound in _parametrizations(fn):
            label = name + ("[" + ",".join(f"{k}={v!r}" for k, v in bound.items()) + "]" if bound else "")
            teardowns = []
            unknown = []
            kwargs = _call_kwargs(fn, bound, teardowns, unknown)
            ran += 1
            if kwargs is None:
                failures += 1
                print(f"[FAIL] {label}: unsupported fixture {unknown[0]!r} "
                      f"(bridge supplies only monkeypatch, tmp_path)")
                continue
            try:
                fn(**kwargs)
                print(f"[OK] {label}")
            except BaseException as exc:  # noqa: BLE001 - surface every failure, never swallow
                failures += 1
                print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
                print(traceback.format_exc())
            finally:
                for undo in reversed(teardowns):
                    try:
                        undo()
                    except Exception:  # noqa: BLE001 - teardown must not mask the test result
                        pass

    if failures:
        print(f"\n[FAIL] {module}: {failures} failure(s) over {ran} case(s)")
        return 1
    print(f"\n[OK] {module}: {ran} case(s) ran, 0 failures")
    return 0
