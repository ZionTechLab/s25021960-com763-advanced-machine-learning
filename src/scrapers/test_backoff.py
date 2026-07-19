"""
Backoff / rate-limit tests — python scrapers/test_backoff.py

These exercise the 429 handling path directly, with a stubbed fetch, so no
network is touched. They exist because a previous fix defined the guard in
one scraper but not the other, and a name-presence check passed anyway: the
module imported cleanly and only failed at runtime, on the rare 429 branch.
Testing the branch is the only thing that actually proves it works.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ikman  # noqa: E402
import riyasewana  # noqa: E402

PASSED = FAILED = 0


def check(label, got, want):
    global PASSED, FAILED
    if got == want:
        PASSED += 1
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def test_constants_defined():
    """Both scrapers must define every constant they reference."""
    print("constants defined in both modules")
    for mod in (riyasewana, ikman):
        name = mod.__name__
        for const in ("DEFAULT_DELAY", "MAX_CONSECUTIVE_429",
                      "MAX_INLINE_SLEEP", "COOLDOWN_SECONDS"):
            check(f"{name}.{const}", hasattr(mod, const), True)
        check(f"{name}.MAX_INLINE_SLEEP sane",
              0 < getattr(mod, "MAX_INLINE_SLEEP", 0) <= 3600, True)


def _run_fetch_with_stub(mod, retry_after, monkey_urls=3):
    """
    Drive the real fetch_details() loop against a stubbed fetch that always
    returns 429 with the given Retry-After. Returns (elapsed_sleep, output).
    """
    slept = []
    printed = []

    real_fetch, real_sleep, real_print, real_db = (
        mod.fetch, mod.time.sleep, None, mod.db_connect
    )

    class FakeConn:
        def execute(self, sql, *a):
            class C:
                rowcount = 0
                def fetchone(self_inner):
                    return (0,)
                def fetchall(self_inner):
                    return [(f"https://x/{i}", "car") for i in range(monkey_urls)]
                def __iter__(self_inner):
                    return iter([])
            return C()
        def commit(self):
            pass

    mod.fetch = lambda *a, **k: (None, 429, retry_after)
    mod.time.sleep = lambda s: slept.append(s)
    mod.db_connect = lambda *a, **k: FakeConn()
    mod.make_session = lambda: None

    import builtins
    orig_print = builtins.print
    builtins.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    try:
        mod.fetch_details(limit=monkey_urls, delay=4.0, max_cooldowns=0)
    finally:
        builtins.print = orig_print
        mod.fetch, mod.time.sleep, mod.db_connect = real_fetch, real_sleep, real_db

    return sum(slept), "\n".join(printed)


def test_long_retry_after_does_not_sleep():
    """
    The regression that prompted this file.

    riyasewana sent Retry-After: 85687 (~23.8 hours). The loop passed that
    straight to time.sleep(), so the process appeared hung for a day while
    looking like a crash. It must now stop and report instead.
    """
    print("long Retry-After stops the run instead of sleeping")
    for mod in (riyasewana, ikman):
        slept, out = _run_fetch_with_stub(mod, retry_after=85687)
        check(f"{mod.__name__}: did not sleep for hours", slept < 3600, True)
        check(f"{mod.__name__}: reported STOPPED", "STOPPED" in out, True)
        check(f"{mod.__name__}: showed resume time", "Resume after" in out, True)


def test_short_retry_after_still_backs_off():
    """A short Retry-After is a normal throttle and should be slept through."""
    print("short Retry-After still backs off normally")
    for mod in (riyasewana, ikman):
        slept, out = _run_fetch_with_stub(mod, retry_after=30)
        check(f"{mod.__name__}: slept", slept > 0, True)
        check(f"{mod.__name__}: no premature stop", "STOPPED: server sent" in out, False)


if __name__ == "__main__":
    for fn in (test_constants_defined,
               test_long_retry_after_does_not_sleep,
               test_short_retry_after_still_backs_off):
        fn()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
