"""Shared fixtures for the review-gate hook tests.
"""

import importlib.util
import pathlib
import sys

import pytest

HOOK_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'review-gate.py')


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """Load the hook from its source path with state redirected to tmp_path.
    """
    monkeypatch.setenv('REVIEW_GATE_HOME', str(tmp_path))
    # A .pyc beside the hook would land in the plugin's scripts/ and
    # ship with the next release.
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    spec = importlib.util.spec_from_file_location('review_gate', HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
