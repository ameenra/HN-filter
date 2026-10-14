"""Smoke tests to verify environment and test harness setup."""

import sys

import hnfilter


def test_package_import():
    """Verify hnfilter package is importable and has a version."""
    assert hnfilter.__file__ is not None
    assert hasattr(hnfilter, "__version__")


def test_environment_sanity():
    """Verify Python version is 3.12 or newer."""
    assert sys.version_info >= (3, 12), f"Expected Python >= 3.12, got {sys.version_info}"
