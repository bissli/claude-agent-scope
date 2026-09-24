"""Tests for the model pin hook in scripts/pin-subagent-model.py.
"""

import pathlib
import re

import pytest

AGENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'agents'


@pytest.mark.parametrize(
    ('subagent_type', 'model'),
    [('agent-scope:sonnet-medium', 'sonnet'),
     ('agent-scope:sonnet-high', 'sonnet'),
     ('Agent-Scope:Sonnet-High', 'Sonnet')])
def test_bare_sonnet_is_removed_on_the_sonnet_tiers(pin, subagent_type, model):
    """Verify a bare sonnet alias on a Sonnet tier falls through to its pin.

    Mutation: passing sonnet through on every type, so the alias outranks
        the claude-sonnet-5 frontmatter pin; or comparing the type raw
        while the gate compares it lowercased.
    Oracle: the hand-written input minus its model key.
    """
    tool_input = {'subagent_type': subagent_type, 'model': model, 'prompt': 'Go.'}
    assert pin.rewrite(tool_input) == {'subagent_type': subagent_type, 'prompt': 'Go.'}


@pytest.mark.parametrize(
    'subagent_type',
    ['agent-scope:opus-high', 'agent-scope:fable-high', 'agent-scope:haiku',
     'Explore', 'Plan', 'claude'])
def test_bare_sonnet_passes_through_off_the_sonnet_tiers(pin, subagent_type):
    """Verify a bare sonnet alias is kept on every type without a Sonnet pin.

    Mutation: removing sonnet on every type, which runs the launch on the
        main-loop model while the gate counts it as an uncapped Sonnet
        launch.
    Oracle: None, the hook's no-change answer.
    """
    tool_input = {'subagent_type': subagent_type, 'model': 'sonnet', 'prompt': 'Go.'}
    assert pin.rewrite(tool_input) is None


@pytest.mark.parametrize(
    'subagent_type', ['agent-scope:opus-high', 'agent-scope:sonnet-high', 'Explore'])
def test_bare_opus_is_removed_on_every_type(pin, subagent_type):
    """Verify the Opus rule still applies whatever the type.

    Mutation: joining the Opus and Sonnet conditions so the Opus alias is
        removed only on a Sonnet tier, which hands every other launch
        that asks for opus to the main-loop Opus version.
    Oracle: the hand-written input minus its model key.
    """
    tool_input = {'subagent_type': subagent_type, 'model': 'opus'}
    assert pin.rewrite(tool_input) == {'subagent_type': subagent_type}


def test_sonnet_tiers_are_the_tiers_that_pin_sonnet(pin):
    """Verify SONNET_TIERS names every definition that pins a Sonnet model.

    Mutation: adding or renaming a Sonnet tier without updating the hook,
        so a bare sonnet alias outranks that tier's pin.
    Oracle: the agents/ files whose model line names claude-sonnet-.
    """
    pinned = {
        f'agent-scope:{path.stem}'
        for path in AGENTS_DIR.glob('*.md')
        if re.search(r'^model: claude-sonnet-', path.read_text(), re.MULTILINE)
        }
    assert pin.SONNET_TIERS == pinned
