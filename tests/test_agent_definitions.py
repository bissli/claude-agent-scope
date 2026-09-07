"""Contract pins for the agent definitions in agents/.

The frontmatter is parsed with a real YAML loader. Claude Code can
register an agent under its file name while its frontmatter fields go
unread, so a text scan that finds the intended `model:` line proves
nothing about what the runtime loaded.
"""

import json
import pathlib
import re

import pytest
import yaml

AGENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'agents'
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTMATTER_RE = re.compile(r'\A---\n(?P<body>.*?)\n---\n', re.DOTALL)
FIELDS = ('name', 'description', 'model', 'effort')
CAPPED_TIERS = ('opus-medium', 'opus-high', 'opus-xhigh', 'fable-xhigh')
DERIVING_TIERS = ('opus-xhigh', 'fable-xhigh')
EXPECTED = {
    'opus-medium': ('claude-opus-5', 'medium', 'Counts as a capped launch'),
    'opus-high': ('claude-opus-5', 'high', 'Three per round is the default cap'),
    'opus-xhigh': (
        'claude-opus-5', 'xhigh', 'derive seat shared with fable-xhigh'),
    'fable-xhigh': (
        'claude-fable-5-1', 'xhigh', 'derive seat shared with opus-xhigh'),
    'sonnet-medium': ('sonnet', 'medium', 'uncapped under the review gate'),
    'sonnet-high': ('sonnet', 'high', 'uncapped under the review gate'),
    'haiku': ('haiku', None, 'uncapped under the review gate'),
    }


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key.

    Notes
    -----
    - Plain safe_load() keeps the last value for a repeated key, so a
      second `model:` would pass unnoticed while the runtime may take
      either one.
    """


def no_duplicate_keys(loader: StrictLoader, node: yaml.MappingNode) -> dict:
    """Construct a mapping, raising on the first repeated key.

    Parameters
    ----------
    loader : StrictLoader
        The loader constructing the node.
    node : yaml.MappingNode
        The mapping node under construction.

    Returns
    -------
    dict
        The constructed mapping.

    Raises
    ------
    yaml.constructor.ConstructorError
        When two keys of the mapping are equal.
    """
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f'duplicate key: {key}', key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=True)


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_duplicate_keys)


def frontmatter(text):
    """Return the parsed frontmatter mapping of one agent definition.

    Parameters
    ----------
    text : str
        The whole file text.

    Returns
    -------
    Any
        Whatever the loader builds from the leading block; a mapping for
        a well-formed definition.

    Raises
    ------
    AssertionError
        When the text does not open with a closed frontmatter block.
    yaml.YAMLError
        When the block is not valid YAML, duplicate keys included.
    """
    match = FRONTMATTER_RE.match(text)
    assert match, 'the file does not open with a closed --- frontmatter block'
    return yaml.load(match.group('body'), Loader=StrictLoader)


def faults(fields, stem, model, effort):
    """Return every contract fault of one parsed frontmatter mapping.

    Parameters
    ----------
    fields : Any
        The parsed frontmatter.
    stem : str
        The file stem the CLI registers the agent under.
    model : str
        The model id or family alias the tier pins.
    effort : str or None
        The effort the tier pins, or None where the tier carries none.

    Returns
    -------
    list[str]
        One message per fault, empty when the mapping meets the contract.
    """
    if not isinstance(fields, dict):
        return [f'frontmatter is {type(fields).__name__}, not a mapping']
    found = []
    for key in ('name', 'description', 'model'):
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            found.append(f'{key} is not a nonempty string: {value!r}')
    if fields.get('name') != stem:
        found.append(f'name {fields.get("name")!r} is not the file stem {stem!r}')
    if fields.get('model') != model:
        found.append(f'model {fields.get("model")!r} is not {model!r}')
    if fields.get('effort') != effort:
        found.append(f'effort {fields.get("effort")!r} is not {effort!r}')
    if effort is None and 'effort' in fields:
        found.append('effort is present on a tier that pins none')
    unexpected = set(fields) - set(FIELDS)
    if unexpected:
        found.append(f'unexpected top-level keys: {sorted(unexpected)}')
    return found


def definition(stem):
    """Return the raw text of one shipped agent definition.
    """
    return (AGENTS_DIR / f'{stem}.md').read_text(encoding='utf-8')


def test_the_tier_files_are_exactly_the_shipped_tiers():
    """Verify the agents/ directory contains exactly the plugin's tiers.

    Mutation: renaming or deleting a tier file, or adding a type the
        directive does not describe.
    Oracle: the hand-listed tier names in EXPECTED.
    """
    assert {path.stem for path in AGENTS_DIR.glob('*.md')} == set(EXPECTED)


def test_tiers_match_gate_constant(gate):
    """Verify EXPECTED names equal the gate's TIERS constant.

    Mutation: adding a tier to EXPECTED or to TIERS without updating the
        other, so the gate denies a type the agent definitions ship.
    Oracle: review_gate.TIERS as loaded from scripts/review-gate.py.
    """
    assert set(EXPECTED) == set(gate.TIERS)


def test_capped_and_deriving_tiers_match_the_gate(gate):
    """Verify the capped and deriving tier lists equal the gate's own.

    Mutation: adding a capped or deriving tier on one side only, so the
        definition tests check the header and derive rules against a set
        the gate does not enforce.
    Oracle: review_gate.CAPPED_TIERS and review_gate.SEAT_TIERS.
    """
    assert set(CAPPED_TIERS) == set(gate.CAPPED_TIERS)
    assert set(DERIVING_TIERS) == set(gate.SEAT_TIERS)


@pytest.mark.parametrize('stem', sorted(EXPECTED))
def test_definition_frontmatter_parses_and_pins_model_and_effort(stem):
    """Verify each definition is valid YAML pinning its name, model, effort.

    Mutation: a colon-space inside an unquoted description or a dedented
        continuation line, either of which leaves the file registered
        under its name with no field read; a name that differs from the
        file stem; a dropped or aliased model pin, which collapses onto
        the main-loop model; an effort on the haiku definition; or prose
        that has become a further top-level key.
    Oracle: a strict YAML load, then the hand-listed model and effort
        per tier.
    """
    fields = frontmatter(definition(stem))
    model, effort, _ = EXPECTED[stem]
    assert faults(fields, stem, model, effort) == []


@pytest.mark.parametrize('stem', sorted(EXPECTED))
def test_description_keeps_every_wrapped_line(stem):
    """Verify the parsed description is the whole folded block.

    Mutation: closing the block scalar early, mis-indenting a
        continuation paragraph, or an editor rewrapping the description
        onto a line the loader reads as a key; each drops text a
        launching agent selects the tier from.
    Oracle: the indented lines of the description block, joined by hand,
        compared to the parsed value.
    """
    body = FRONTMATTER_RE.match(definition(stem)).group('body')
    block = body.partition('description: >-\n')[2]
    wrapped = []
    for line in block.split('\n'):
        if not line.startswith('  '):
            break
        wrapped.append(line.strip())
    assert len(wrapped) > 1
    assert frontmatter(definition(stem))['description'] == ' '.join(wrapped)


@pytest.mark.parametrize('stem', sorted(EXPECTED))
def test_description_carries_its_gate_sentence(stem):
    """Verify each description tells a launching agent how the gate treats it.

    Mutation: dropping the sentence that says a tier counts against the
        capped budget, shares a derive seat, or is uncapped, so a
        launching agent learns the accounting only from a denial.
    Oracle: the hand-listed gate phrase per tier.
    """
    assert EXPECTED[stem][2] in frontmatter(definition(stem))['description']


@pytest.mark.parametrize('stem', sorted(CAPPED_TIERS))
def test_capped_definitions_name_the_header_rule(stem):
    """Verify each capped tier states the header and the swarm round.

    Mutation: dropping the header sentence from a capped description, so
        a launching agent learns the rule only from the deny.
    Oracle: the phrases review-gate header and round: swarm in the
        description.
    """
    description = frontmatter(definition(stem))['description']
    assert 'review-gate header' in description
    assert 'round: swarm' in description


@pytest.mark.parametrize('stem', sorted(DERIVING_TIERS))
def test_deriving_definitions_state_the_derive_contract(stem):
    """Verify each deriving tier asks for derive in every round.

    Mutation: describing derive as a review-round field, so a synthesize
        launch omits it and is denied; or dropping the model-option
        sentence, so a launch names an alias that outranks the pin.
    Oracle: the phrases derive: <kind>, every round, synthesize
        included, and the model-option sentence in the description.
    """
    description = frontmatter(definition(stem))['description']
    assert 'derive: <kind> in every round, synthesize included' in description
    assert 'Omit the model option' in description


def test_a_colon_in_an_unquoted_description_is_rejected():
    """Verify the loader rejects the frontmatter fault this release repaired.

    Mutation: replacing the strict YAML load with a line scan that
        partitions on the first colon, which reads the right model and
        effort out of a document Claude Code cannot parse at all.
    Oracle: a fixture whose model and effort lines are correct and whose
        unquoted description carries a colon-space raises a YAML error.
    """
    text = (
        '---\n'
        'name: probe\n'
        'description: Opus at medium effort: the prompt hands over everything.\n'
        'model: claude-opus-5\n'
        'effort: medium\n'
        '---\n\nBody.\n')
    with pytest.raises(yaml.YAMLError):
        frontmatter(text)


def test_a_dedented_continuation_is_rejected():
    """Verify a continuation paragraph outside the block scalar is rejected.

    Mutation: reading only the first line of a description, which hides a
        second paragraph the loader cannot place.
    Oracle: a fixture whose second paragraph starts at column 1 raises a
        YAML error.
    """
    text = (
        '---\n'
        'name: probe\n'
        'description: >-\n'
        '  First paragraph.\n'
        'Second paragraph the loader cannot place.\n'
        'model: claude-opus-5\n'
        '---\n\nBody.\n')
    with pytest.raises(yaml.YAMLError):
        frontmatter(text)


def test_a_pin_folded_into_the_description_is_a_fault():
    """Verify an over-indented model line is caught, not read as a pin.

    Mutation: searching the whole frontmatter text for a model line
        instead of reading the parsed mapping, so a model swallowed by
        the description passes as a pin while the runtime sees none.
    Oracle: the fixture parses, its description ends with the model
        text, and faults() names the missing model.
    """
    text = (
        '---\n'
        'name: probe\n'
        'description: >-\n'
        '  First paragraph.\n'
        '  model: claude-opus-5\n'
        'effort: medium\n'
        '---\n\nBody.\n')
    fields = frontmatter(text)
    assert fields['description'].endswith('model: claude-opus-5')
    assert any('model' in fault for fault in faults(
        fields, 'probe', 'claude-opus-5', 'medium'))


def test_a_duplicate_key_is_rejected():
    """Verify a repeated key is refused rather than resolved to the last one.

    Mutation: loading with plain safe_load(), which keeps the last value
        and hides a second model line the runtime may read either way.
    Oracle: a fixture with two model keys raises a YAML error.
    """
    text = (
        '---\n'
        'name: probe\n'
        'description: >-\n'
        '  A description.\n'
        'model: claude-opus-5\n'
        'model: sonnet\n'
        '---\n\nBody.\n')
    with pytest.raises(yaml.YAMLError):
        frontmatter(text)


def test_a_folded_description_carries_the_header_fields():
    """Verify header text inside a folded description survives the load.

    Mutation: quoting or escaping the description so that derive: and
        round: read as YAML keys, which is what the block scalar exists
        to prevent.
    Oracle: a fixture whose folded description carries derive: <kind>
        and round: swarm parses to one description holding both, with
        the pins at the top level.
    """
    text = (
        '---\n'
        'name: probe\n'
        'description: >-\n'
        '  Supply a review-gate header and derive: <kind> in every round,\n'
        '  synthesize included; use round: swarm outside review work.\n'
        'model: claude-fable-5-1\n'
        'effort: xhigh\n'
        '---\n\nBody.\n')
    fields = frontmatter(text)
    assert faults(fields, 'probe', 'claude-fable-5-1', 'xhigh') == []
    assert 'derive: <kind>' in fields['description']
    assert 'round: swarm' in fields['description']


def test_plugin_name_matches_gate_constant(gate):
    """Verify plugin.json "name" equals review_gate.PLUGIN_NAME.

    Mutation: renaming the plugin in plugin.json without updating
        PLUGIN_NAME in the gate, so the gate builds the wrong prefix.
    Oracle: review_gate.PLUGIN_NAME as loaded from scripts/review-gate.py.
    """
    plugin_json = REPO_ROOT / '.claude-plugin' / 'plugin.json'
    name = json.loads(plugin_json.read_text(encoding='utf-8'))['name']
    assert name == gate.PLUGIN_NAME


def test_the_three_shipped_descriptions_agree():
    """Verify plugin.json and both marketplace strings carry one description.

    Mutation: correcting the description in plugin.json and leaving the
        marketplace's own two copies stale, which is how the tier count
        went wrong in the first place.
    Oracle: the three strings compared to each other, and the count of
        tiers in EXPECTED spelled out as the word each string opens with.
    """
    plugin = json.loads(
        (REPO_ROOT / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    marketplace = json.loads(
        (REPO_ROOT / '.claude-plugin' / 'marketplace.json').read_text(
            encoding='utf-8'))
    entries = [plugin['description'], marketplace['description']]
    entries += [entry['description'] for entry in marketplace['plugins']]
    assert len(entries) == 3
    assert len(set(entries)) == 1
    spelled = {5: 'Five', 6: 'Six', 7: 'Seven', 8: 'Eight', 9: 'Nine'}
    assert entries[0].startswith(f'{spelled[len(EXPECTED)]} ')


def test_hook_commands_resolve():
    """Verify every command in hooks/hooks.json names an existing script.

    Mutation: moving a script without updating hooks.json, dropping one
        of the four commands, or hardcoding a path in place of the
        ${CLAUDE_PLUGIN_ROOT} placeholder.
    Oracle: four commands - two PreToolUse scripts and two SessionStart
        directive injections - each carrying the placeholder and each
        resolving to a file under the repo root.
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

