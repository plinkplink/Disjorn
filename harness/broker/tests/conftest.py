"""Pytest hook point for broker tests — fixtures live in broker_testlib
(uniquely named so multi-rootdir collection with other harness suites
doesn't collide on the module name `conftest`)."""

from broker_testlib import *  # noqa: F401,F403


def pytest_configure(config):
    """NAME THE SKIPS, do not tally them. This suite reported "485 passed, 4
    skipped" for weeks and nobody asked which four; they were the adapter-drift
    tests, resolving nothing. A count that does not vary with what it measures
    is not an instrument. `-r s` is pytest's own machinery for it; this turns
    it on by default and adds nothing when an -r already names skips."""
    chars = config.option.reportchars or ""
    if not set("saA") & set(chars):
        config.option.reportchars = chars + "s"
