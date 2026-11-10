"""Tests for the model pin hook in scripts/pin-subagent-model.py.
"""

import pytest


@pytest.mark.parametrize(
    ('tool_input', 'expected'),
    [({'subagent_type': 'Explore', 'prompt': 'Go.'},
      {'subagent_type': 'Explore', 'prompt': 'Go.', 'model': 'haiku'}),
     ({'subagent_type': 'Explore', 'model': 'opus'}, None),
     ({'subagent_type': 'agent-scope:sonnet-high', 'model': 'sonnet'}, None),
     ({'subagent_type': 'agent-scope:opus-high', 'model': 'opus'}, None),
     ({'subagent_type': 'Plan'}, None)])
def test_only_a_bare_explore_launch_is_rewritten(pin, tool_input, expected):
    """Verify Explore with no model gets haiku and every other launch is kept.

    Mutation: overriding a model the caller named on Explore, defaulting
        another type to haiku, or keeping the old alias strip that drops
        a model option from a tier.
    Oracle: hand-written inputs and the hook's no-change answer, None.
    """
    assert pin.rewrite(tool_input) == expected
