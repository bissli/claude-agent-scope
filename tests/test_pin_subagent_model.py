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
     ('agent-scope:haiku', 'haiku'),
     ('Agent-Scope:Sonnet-High', 'Sonnet')])
def test_own_family_alias_is_removed_on_a_pinned_tier(pin, subagent_type, model):
    """Verify a bare alias of a tier's own family falls through to its pin.

    Mutation: passing the alias through, so it outranks the tier's
        frontmatter pin; or comparing the type raw while the gate
        compares it lowercased.
    Oracle: the hand-written input minus its model key.
    """
    tool_input = {'subagent_type': subagent_type, 'model': model, 'prompt': 'Go.'}
    assert pin.rewrite(tool_input) == {'subagent_type': subagent_type, 'prompt': 'Go.'}


@pytest.mark.parametrize(
    ('subagent_type', 'model'),
    [('agent-scope:opus-high', 'sonnet'),
     ('agent-scope:fable-high', 'sonnet'),
     ('agent-scope:haiku', 'sonnet'),
     ('Explore', 'sonnet'),
     ('Plan', 'sonnet'),
     ('claude', 'sonnet'),
     ('agent-scope:sonnet-high', 'haiku'),
     ('agent-scope:fable-high', 'haiku'),
     ('Explore', 'haiku')])
def test_alias_passes_through_off_its_own_tier(pin, subagent_type, model):
    """Verify sonnet and haiku are kept on every type pinned to another model.

    Mutation: removing the alias on every type, or on a tier of another
        family, which runs the launch on that type's own pin, such as
        Fable, or on the main-loop model while the gate counts it as an
        uncapped launch.
    Oracle: None, the hook's no-change answer.
    """
    tool_input = {'subagent_type': subagent_type, 'model': model, 'prompt': 'Go.'}
    assert pin.rewrite(tool_input) is None


@pytest.mark.parametrize(
    'subagent_type', ['agent-scope:opus-high', 'agent-scope:sonnet-high', 'Explore'])
def test_bare_opus_is_removed_on_every_type(pin, subagent_type):
    """Verify the Opus rule still applies whatever the type.

    Mutation: joining the Opus and tier-family conditions so the Opus
        alias is removed only on a pinned tier, which hands every other
        launch that asks for opus to the main-loop Opus version.
    Oracle: the hand-written input minus its model key.
    """
    tool_input = {'subagent_type': subagent_type, 'model': 'opus'}
    assert pin.rewrite(tool_input) == {'subagent_type': subagent_type}


def test_family_by_pinned_tier_matches_the_definitions(pin):
    """Verify FAMILY_BY_PINNED_TIER names every Sonnet and Haiku pin.

    Mutation: adding, renaming, or repinning a Sonnet or Haiku tier
        without updating the hook, so a bare alias outranks that tier's
        pin or is removed from a tier of another family.
    Oracle: the family named on each agents/ file's model line.
    """
    family_by_tier = {}
    for path in AGENTS_DIR.glob('*.md'):
        match = re.search(
            r'^model: claude-(sonnet|haiku)-', path.read_text(), re.MULTILINE)
        if match:
            family_by_tier[f'agent-scope:{path.stem}'] = match.group(1)
    assert pin.FAMILY_BY_PINNED_TIER == family_by_tier
