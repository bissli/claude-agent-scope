"""Shared fixtures for the hook script tests.
"""

import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

HOOK_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'review-gate.py')
OUTCOME_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'record-outcome.py')
PIN_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'pin-subagent-model.py')


def load_script(
        monkeypatch: pytest.MonkeyPatch,
        path: pathlib.Path,
        module_name: str) -> ModuleType:
    """Load one hook script from its source path as a fresh module.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Turns bytecode writing off for the calling test.
    path : pathlib.Path
        The script under scripts/; its hyphenated name rules out import.
    module_name : str
        The name the module registers under.

    Returns
    -------
    ModuleType
        The executed module.
    """
    # A .pyc beside the hook would land in the plugin's scripts/ and
    # ship with the next release.
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """Load the hook from its source path with state redirected to tmp_path.
    """
    monkeypatch.setenv('REVIEW_GATE_HOME', str(tmp_path))
    return load_script(monkeypatch, HOOK_PATH, 'review_gate')


@pytest.fixture
def outcome(tmp_path, monkeypatch):
    """Load record-outcome.py with state redirected to tmp_path.

    The REVIEW_GATE_HOME env var controls where outcome.jsonl is written,
    matching the gate fixture so both modules share the same temp dir.
    """
    monkeypatch.setenv('REVIEW_GATE_HOME', str(tmp_path))
    return load_script(monkeypatch, OUTCOME_PATH, 'record_outcome')


@pytest.fixture
def pin(monkeypatch):
    """Load the model pin hook from its source path.
    """
    return load_script(monkeypatch, PIN_PATH, 'pin_subagent_model')
