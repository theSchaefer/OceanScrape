"""CLI tests: distributed subcommands plus single-server compatibility.

Verifies the new ``run.py worker|enqueue|serve`` dispatch works and — crucially —
that the existing single-server scraper CLI still runs unchanged. The scraper /
enqueue checks need the capture+DB stack, so they skip cleanly where psycopg2
isn't installed (e.g. a dev box) and run on the real hosts.

Run with::

    .venv/Scripts/python.exe -m pytest tests/test_worker_cli.py
    .venv/Scripts/python.exe tests/test_worker_cli.py   # standalone
"""

import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_HAS_PSYCOPG2 = importlib.util.find_spec("psycopg2") is not None


class SkipTest(Exception):
    pass


def _skip(reason):
    try:
        import pytest
        pytest.skip(reason)
    except ImportError:
        raise SkipTest(reason)


def _run(args, **kw):
    return subprocess.run([sys.executable, *args], cwd=ROOT,
                          capture_output=True, text=True, **kw)


def test_run_py_serve_help():
    r = _run(["run.py", "serve", "--help"])
    assert r.returncode == 0, r.stderr
    assert "control-plane" in r.stdout.lower()


def test_run_py_worker_help():
    r = _run(["run.py", "worker", "--help"])
    assert r.returncode == 0, r.stderr
    assert "--server" in r.stdout and "--token-env" in r.stdout


def test_run_py_enqueue_help():
    r = _run(["run.py", "enqueue", "--help"])
    assert r.returncode == 0, r.stderr
    assert "--batch-size" in r.stdout


def test_run_py_continuous_help():
    r = _run(["run.py", "continuous", "--help"])
    assert r.returncode == 0, r.stderr
    assert "--state-file" in r.stdout
    assert "--continue-on-wave-failure" in r.stdout


def test_single_server_scraper_cli_still_runs():
    """The original scraper CLI must keep working (no DB / network needed)."""
    if not _HAS_PSYCOPG2:
        _skip("psycopg2 not installed in this environment")
        return
    r = _run(["scraper_global.py", "--dry-run-grid"])
    assert r.returncode == 0, r.stderr
    assert "Global tile grid" in r.stdout


def test_enqueue_dry_run_builds_batches():
    if not _HAS_PSYCOPG2:
        _skip("psycopg2 not installed in this environment")
        return
    r = _run(["run.py", "enqueue", "--regions", "global",
              "--batch-size", "12", "--dry-run"])
    assert r.returncode == 0, r.stderr
    assert "would enqueue" in r.stdout


def _run_all():
    failures = 0
    skipped = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except SkipTest as exc:
                skipped += 1
                print(f"SKIP {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'OK' if not failures else 'FAILED'} "
          f"({failures} failures, {skipped} skipped)")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
