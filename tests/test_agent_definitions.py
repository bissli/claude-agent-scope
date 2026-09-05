"""Contract pins for the agent definitions in agents/.
"""

import json
import pathlib
import re

import pytest

AGENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'agents'
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTMATTER_RE = re.compile(r'\A---\n(?P<body>.*?)\n---\n', re.DOTALL)
EXPECTED = {
    'opus-medium': ('claude-opus-5', 'medium', 'Counts as an Opus reviewer'),
    'opus-high': ('claude-opus-5', 'high', 'Three per round is the default cap'),
    'opus-xhigh': ('claude-opus-5', 'xhigh', 'declares derive:'),
    'sonnet-medium': ('sonnet', 'medium', 'Never takes an Opus slot'),
    'sonnet-high': ('sonnet', 'high', 'Never takes an Opus slot'),
    'haiku': ('haiku', None, 'Never takes an Opus slot'),
    }


def frontmatter(path):
    """Return the YAML frontmatter of an agent file as a flat dict.
    """
    match = FRONTMATTER_RE.match(path.read_text(encoding='utf-8'))
    assert match, f'{path.name} has no frontmatter'
    fields = {}
    for line in match.group('body').splitlines():
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip()
    return fields


def test_the_tier_files_are_exactly_the_six_tiers():
    """Verify the agents/ directory contains exactly the six plugin tiers.

    Mutation: renaming or deleting a tier file, or adding a type the
        directive does not describe.
    Oracle: the hand-listed six tier names.
    """
    assert {path.stem for path in AGENTS_DIR.glob('*.md')} == set(EXPECTED)


def test_tiers_match_gate_constant(gate):
    """Verify EXPECTED names equal the gate's TIERS constant.

    Mutation: adding a tier to EXPECTED or to TIERS without updating the
        other, so the gate denies a type the agent definitions ship.
    Oracle: review_gate.TIERS as loaded from scripts/review-gate.py.
    """
    assert set(EXPECTED) == set(gate.TIERS)


@pytest.mark.parametrize('stem', sorted(EXPECTED))
def test_definition_pins_name_model_effort_and_gate_sentence(stem):
    """Verify each type pins name, model, effort, and its gate sentence.

    Mutation: a name that differs from the file stem (the CLI registers
        the name, so the type the directive names would not exist), a
        dropped or wrong model pin, an effort on the haiku definition, an
        Opus pin by alias (which collapses onto the main-loop model), an
        effort value outside the CLI's list, or a description that drops
        the sentence telling a launching agent how the gate treats it.
    Oracle: the hand-listed model, effort, and gate phrase per type.
    """
    fields = frontmatter(AGENTS_DIR / f'{stem}.md')
    model, effort, gate_phrase = EXPECTED[stem]
    assert fields['name'] == stem
    assert fields['model'] == model
    assert fields.get('effort') == effort
    assert gate_phrase in fields['description']


@pytest.mark.parametrize('stem', ['opus-medium', 'opus-high', 'opus-xhigh'])
def test_opus_definitions_name_the_header_rule(stem):
    """Verify each Opus tier tells a launching agent to open with the header.

    Mutation: dropping the header sentence from an Opus description, so a
        launching agent learns the rule only from the deny.
    Oracle: the phrase round: swarm in the description.
    """
    assert 'round: swarm' in frontmatter(AGENTS_DIR / f'{stem}.md')['description']


def test_plugin_name_matches_gate_constant(gate):
    """Verify plugin.json "name" equals review_gate.PLUGIN_NAME.

    Mutation: renaming the plugin in plugin.json without updating
        PLUGIN_NAME in the gate, so the gate builds the wrong prefix.
    Oracle: review_gate.PLUGIN_NAME as loaded from scripts/review-gate.py.
    """
    plugin_json = REPO_ROOT / '.claude-plugin' / 'plugin.json'
    name = json.loads(plugin_json.read_text(encoding='utf-8'))['name']
    assert name == gate.PLUGIN_NAME


def test_hook_commands_resolve():
    """Verify every command in hooks/hooks.json names an existing script.

    Mutation: moving a script without updating hooks.json, dropping one
        of the four commands, or hardcoding a path in place of the
        ${CLAUDE_PLUGIN_ROOT} placeholder.
    Oracle: four commands, each carrying the placeholder, each resolving
        to a file under the repo root.
    """
    hooks_json = REPO_ROOT / 'hooks' / 'hooks.json'
    hooks_data = json.loads(hooks_json.read_text(encoding='utf-8'))
    commands = []
    for event_hooks in hooks_data.get('hooks', {}).values():
        for entry in event_hooks:
            for hook in entry.get('hooks', []):
                cmd = hook.get('command', '')
                commands.append(cmd)
    assert len(commands) == 4
    checked = [cmd for cmd in commands if '${CLAUDE_PLUGIN_ROOT}' in cmd]
    assert len(checked) == 4
    for cmd in checked:
        relative = cmd.split('${CLAUDE_PLUGIN_ROOT}/', 1)[-1]
        relative = relative.split('"')[0].strip()
        assert (REPO_ROOT / relative).exists(), (
            f'command references missing file: {relative}')

