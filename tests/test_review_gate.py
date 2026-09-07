"""Tests for the Agent side of the review-gate PreToolUse hook.

Run from the repo root: `poetry run pytest tests`. The `gate` fixture in
conftest.py loads scripts/review-gate.py with its state and log redirected
to a temp dir through REVIEW_GATE_HOME.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import HOOK_PATH

INTERPRETERS = {}
for candidate in (
        sys.executable,
        shutil.which('python3'),
        '/usr/bin/python3',
        *(shutil.which(f'python3.{minor}') for minor in range(10, 15))):
    if candidate and os.path.exists(candidate):
        INTERPRETERS.setdefault(os.path.realpath(candidate), candidate)


def header(round_name='review', opus_cap='3', derive=None):
    """Return a marked prompt: the header, a blank line, then a brief.
    """
    lines = ['<review-gate>', f'round: {round_name}']
    if opus_cap is not None:
        lines.append(f'opus-cap: {opus_cap}')
    if derive is not None:
        lines.append(f'derive: {derive}')
    return '\n'.join([*lines, '</review-gate>', '', 'Review this text.'])


def agent_input(
    prompt,
    *,
    model=None,
    subagent_type='agent-scope:opus-high',
    prompt_id='turn-1',
    session='session-1',
    transcript_path=None):
    """Build a PreToolUse payload for one Agent launch.

    Parameters
    ----------
    prompt : str
        The Agent prompt.
    model : str or None
        The Agent model alias; omitted from tool_input when None.
    subagent_type : str
        The Agent type; agent-scope:opus-high is the tier most tests launch.
    prompt_id : str or None
        The cycle key; omitted from the payload when None.
    session : str
        The session id.
    transcript_path : pathlib.Path or None
        Added to the payload when given, for the fallback key.

    Returns
    -------
    dict
        The payload as Claude Code would send it on stdin.
    """
    payload = {
        'session_id': session,
        'tool_name': 'Agent',
        'tool_input': {
            'prompt': prompt,
            'description': 'reviewer',
            'subagent_type': subagent_type,
            },
        }
    if prompt_id is not None:
        payload['prompt_id'] = prompt_id
    if transcript_path is not None:
        payload['transcript_path'] = str(transcript_path)
    if model is not None:
        payload['tool_input']['model'] = model
    return payload


def xhigh(round_name='review', opus_cap='6', derive='proof', **kwargs):
    """Build an opus-xhigh launch with a kind, at the smallest cap with a seat.
    """
    prompt = header(round_name, opus_cap, derive)
    return agent_input(prompt, subagent_type='agent-scope:opus-xhigh', **kwargs)


def fable(round_name='review', opus_cap='6', derive='joint-behavior', **kwargs):
    """Build a fable-xhigh launch with a kind, at the smallest cap with a seat.
    """
    prompt = header(round_name, opus_cap, derive)
    return agent_input(prompt, subagent_type='agent-scope:fable-xhigh', **kwargs)


def decision(output):
    """Return the permissionDecision of a hook result, or None for an allow.

    An allow is silence, or a systemMessage envelope with no decision.
    """
    if output is None or 'hookSpecificOutput' not in output:
        return None
    return output['hookSpecificOutput']['permissionDecision']


def seat_message(output):
    """Return the systemMessage of an allowed opus-xhigh result.
    """
    assert 'hookSpecificOutput' not in output
    return output['systemMessage']


def reason(output):
    """Return the permissionDecisionReason of a deny result.
    """
    assert output is not None, 'expected a deny, got an allow'
    return output['hookSpecificOutput']['permissionDecisionReason']


def transcript_line(kind, uuid, content, *, origin=None, prompt_id=None, meta=False):
    """Return one transcript JSONL record.

    origin becomes origin.kind, prompt_id becomes promptId, and meta sets
    isMeta, each omitted when not given, as the CLI writes them.
    """
    record = {'type': kind, 'uuid': uuid, 'message': {'content': content}}
    if origin is not None:
        record['origin'] = {'kind': origin}
    if prompt_id is not None:
        record['promptId'] = prompt_id
    if meta:
        record['isMeta'] = True
    return json.dumps(record)


def last_log(gate):
    """Return the last gate.jsonl line as a dict.
    """
    return json.loads((gate.STATE_HOME / 'gate.jsonl').read_text().splitlines()[-1])


def state_file(gate, session='session-1'):
    """Return the path of a session's state file.
    """
    return gate.STATE_HOME / f'{session}.json'


def stored_cycle(gate, session='session-1', turn='turn-1'):
    """Return one cycle's stored counters, or None when none is stored.
    """
    path = state_file(gate, session)
    if not path.exists():
        return None
    return json.loads(path.read_text()).get('cycles', {}).get(turn)


def reload_gate():
    """Load a second instance of the hook, as the next process would.
    """
    spec = importlib.util.spec_from_file_location('review_gate_again', HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exhaust_default_cap(gate, prompt=None):
    """Take the three default slots and return the fourth, denied, result.
    """
    payload = agent_input(prompt or header())
    assert all(gate.gate_agent(payload) is None for _ in range(3))
    return gate.gate_agent(payload)


# --- Header parsing ---


@pytest.mark.parametrize(
    ('subagent_type', 'model'),
    [('agent-scope:opus-high', None), ('agent-scope:opus-xhigh', None), ('Plan', None),
     ('claude-code-guide', None), ('Explore', 'opus')])
def test_unmarked_opus_launch_is_denied(gate, subagent_type, model):
    """Verify an Opus-tier Agent whose prompt carries no header is denied.

    Mutation: allowing an unmarked launch on every type, or testing the
        three tier names instead of is_uncapped so that Plan, a plugin
        type, or an Explore naming opus slips through unmarked.
    Oracle: five Opus-tier shapes are denied naming round: swarm; no
        slot is logged and no state is written.
    """
    payload = agent_input(
        'Implement the change.', model=model, subagent_type=subagent_type)
    output = gate.gate_agent(payload)
    assert decision(output) == 'deny'
    assert 'round: swarm' in reason(output)
    assert 'round_n' not in last_log(gate)
    assert not state_file(gate).exists()


@pytest.mark.parametrize(
    ('subagent_type', 'model'),
    [('agent-scope:sonnet-high', None), ('agent-scope:haiku', None), ('Explore', None),
     ('agent-scope:opus-high', 'sonnet')])
def test_unmarked_cheap_launch_is_outside_review_policy(gate, subagent_type, model):
    """Verify a cheap Agent whose prompt carries no header is never gated.

    Mutation: requiring the header on every launch whatever its model.
    Oracle: twelve unmarked launches on each of four cheap shapes all
        pass and log scope not-review-marked.
    """
    payload = agent_input(
        'Implement the change.', model=model, subagent_type=subagent_type)
    assert all(gate.gate_agent(payload) is None for _ in range(12))
    assert last_log(gate)['scope'] == 'not-review-marked'


@pytest.mark.parametrize('model', ['sonnet', 'haiku'])
def test_fork_is_opus_tier_whatever_its_model(gate, model):
    """Verify a fork is counted whatever model it names.

    Mutation: testing the model before the type in is_uncapped, so a
        fork naming sonnet passes unmarked and uncounted although the
        runtime ignores a fork's model and runs it on the main-loop model.
    Oracle: an unmarked fork naming sonnet or haiku is denied naming
        round: swarm; a marked one takes review slot 1 of 3.
    """
    unmarked = agent_input('Implement the change.', model=model, subagent_type='fork')
    assert 'round: swarm' in reason(gate.gate_agent(unmarked))
    marked = agent_input(header(), model=model, subagent_type='fork')
    assert gate.gate_agent(marked) is None
    assert (last_log(gate)['round_n'], last_log(gate)['round_cap']) == (1, 3)


def test_type_names_compare_lowercased(gate):
    """Verify a case-variant type name meets the same rules as the exact name.

    Mutation: comparing subagent_type raw while the model alias is
        lowercased, so General-purpose passes the tier rule and
        agent-scope:Opus-XHigh escapes the derive rule and its seat.
    Oracle: General-purpose with a valid header is denied naming the tier;
        agent-scope:Opus-XHigh with no derive is denied naming the kind;
        the log keeps the raw name.
    """
    output = gate.gate_agent(agent_input(header(), subagent_type='General-purpose'))
    assert 'name the tier' in reason(output)
    output = gate.gate_agent(
        agent_input(header(), subagent_type='agent-scope:Opus-XHigh'))
    assert 'needs derive: <kind>' in reason(output)
    assert last_log(gate)['agent_type'] == 'agent-scope:Opus-XHigh'


def test_swarm_round_has_its_own_counter(gate):
    """Verify the swarm round is capped like review, on its own counter.

    Mutation: letting a swarm launch through uncounted, or charging it to
        the review counter.
    Oracle: after three reviews fill the review cap at opus-cap 3, three
        swarm launches pass and the fourth is denied at most 3 in the swarm
        round; the state holds review 3 and swarm 3.
    """
    exhaust_default_cap(gate)
    swarm = agent_input(header('swarm', '3'))
    assert all(gate.gate_agent(swarm) is None for _ in range(3))
    output = gate.gate_agent(swarm)
    assert decision(output) == 'deny'
    assert 'at most 3 capped agents in the swarm round' in reason(output)
    counters = stored_cycle(gate)
    assert (counters['review'], counters['swarm']) == (3, 3)


def test_swarm_launch_fixes_the_opus_cap(gate):
    """Verify the first swarm agent fixes the cycle's opus-cap like a review.

    Mutation: leaving swarm out of the opus-cap rounds, so a swarm
        fan-out never declares and never fixes an opus-cap.
    Oracle: a swarm launch with no opus-cap is denied naming the three
        rounds; one declaring 6 fixes it, and a review at 3 is then
        denied as a mismatch.
    """
    output = gate.gate_agent(agent_input(header('swarm', None)))
    assert decision(output) == 'deny'
    assert 'review, verify, and swarm agents require opus-cap' in reason(output)
    assert gate.gate_agent(agent_input(header('swarm', '6'))) is None
    output = gate.gate_agent(agent_input(header()))
    assert decision(output) == 'deny'
    assert 'declared opus-cap 6' in reason(output)


def test_swarm_round_draws_on_the_cycle_seat(gate):
    """Verify the swarm round applies the derive rule and shares the seat.

    Mutation: exempting the swarm round from the derive rule, or keying
        the seat on the round so swarm and review each get one at 6.
    Oracle: a swarm launch on opus-xhigh with no derive is denied naming
        the kind; with one it passes; an opus-xhigh review launch in the
        same cycle is then denied as seat #2 of 1.
    """
    no_kind = agent_input(header('swarm'), subagent_type='agent-scope:opus-xhigh')
    output = gate.gate_agent(no_kind)
    assert decision(output) == 'deny'
    assert 'needs derive: <kind>' in reason(output)
    assert decision(gate.gate_agent(xhigh('swarm'))) is None
    output = gate.gate_agent(xhigh('review'))
    assert decision(output) == 'deny'
    assert 'holds 1 derive seat; this would be #2' in reason(output)


def test_only_leading_header_is_control_metadata(gate):
    """Verify only a block at the very start of the prompt is parsed.

    Mutation: searching the whole prompt for the block instead of
        matching at its start.
    Oracle: a header preceded by one line of text is not a header, so an
        Opus launch is denied as unmarked and twelve sonnet-high launches
        all pass; a header at the start followed by a quoted conflicting
        block still allows.
    """
    quoted = '\n\nQuoted:\n<review-gate>\nround: verify\n</review-gate>'
    quoted_later = header() + quoted
    assert gate.gate_agent(agent_input(quoted_later)) is None
    not_leading = 'Please review this doc:\n' + header()
    assert 'round: swarm' in reason(gate.gate_agent(agent_input(not_leading)))
    cheap = agent_input(not_leading, subagent_type='agent-scope:sonnet-high')
    assert all(gate.gate_agent(cheap) is None for _ in range(12))


@pytest.mark.parametrize('model', [None, 'sonnet', 'haiku', 'opus'])
@pytest.mark.parametrize('subagent_type', [None, 'general-purpose'])
def test_inheriting_launches_are_denied_whatever_the_model(gate, subagent_type, model):
    """Verify a launch with no type, or on general-purpose, is always denied.

    Mutation: exempting a cheap model, applying the rule only to marked
        prompts, or treating an omitted type as anything but inheriting.
    Oracle: eight combinations of type and model on an unmarked prompt
        are denied with a reason naming the tiers; no slot is logged.
    """
    payload = agent_input('Implement the change.', model=model)
    if subagent_type is None:
        del payload['tool_input']['subagent_type']
    else:
        payload['tool_input']['subagent_type'] = subagent_type
    output = gate.gate_agent(payload)
    assert decision(output) == 'deny'
    assert 'name the tier' in reason(output)
    assert 'round_n' not in last_log(gate)


@pytest.mark.parametrize(
    'subagent_type',
    ['agent-scope:opus-medium', 'agent-scope:opus-xhigh', 'claude-code-guide'])
def test_named_types_pass_the_tier_rule(gate, subagent_type):
    """Verify any named type other than general-purpose passes the tier rule.

    Mutation: allowing only the prefixed tier names, which would block
        Explore, Plan, and other named types.
    Oracle: a swarm-marked launch on each named type allows.
    """
    derive = 'proof' if subagent_type == 'agent-scope:opus-xhigh' else None
    prompt = header('swarm', '6' if derive else '3', derive)
    output = gate.gate_agent(agent_input(prompt, subagent_type=subagent_type))
    assert decision(output) is None


def test_bare_tier_name_with_header_denied_and_takes_no_slot(gate):
    """Verify a bare tier name is denied even when the header is valid.

    Mutation: dropping the bare-name branch so a bare tier with a valid
        header is allowed and counted, consuming a slot.
    Oracle: bare 'opus-high' with a valid review header is denied naming
        the plugin prefix; three subsequent agent-scope:opus-high launches
        all allow, proving no slot was taken.
    """
    output = gate.gate_agent(agent_input(header(), subagent_type='opus-high'))
    assert decision(output) == 'deny'
    assert "plugin's agents" in reason(output)
    assert 'agent-scope:opus-high' in reason(output)
    assert 'not opus-high' in reason(output)
    assert all(gate.gate_agent(agent_input(header())) is None for _ in range(3))


def test_prefixed_nonexistent_tier_denied_and_takes_no_slot(gate):
    """Verify a prefixed name that is not a tier is denied before a slot.

    Mutation: dropping the scoped-non-tier branch so a typo like
        agent-scope:opus-hgih with a valid header is allowed and counted.
    Oracle: agent-scope:opus-hgih with a valid header is denied naming
        the tiers; three subsequent agent-scope:opus-high launches all
        allow, proving no slot was taken.
    """
    output = gate.gate_agent(
        agent_input(header(), subagent_type='agent-scope:opus-hgih'))
    assert decision(output) == 'deny'
    assert 'agent-scope:opus-hgih is not a tier' in reason(output)
    assert 'agent-scope:opus-medium' in reason(output)
    assert all(gate.gate_agent(agent_input(header())) is None for _ in range(3))


@pytest.mark.parametrize(
    'subagent_type', ['agent-scope:', 'agent-scope:general-purpose'])
def test_prefixed_inheriting_names_are_denied_as_non_tiers(gate, subagent_type):
    """Verify a prefixed empty or general-purpose name is denied as not a tier.

    Mutation: testing INHERITING_TYPES on the stripped name, so
        agent-scope:general-purpose is refused for inheriting the session
        effort, a reason that is false of a name no agent carries.
    Oracle: the denial names the launch as not a tier, lists the seven,
        and never says inherit.
    """
    output = gate.gate_agent(agent_input(header(), subagent_type=subagent_type))
    assert decision(output) == 'deny'
    assert f'{subagent_type} is not a tier' in reason(output)
    assert 'agent-scope:opus-medium' in reason(output)
    assert 'inherit' not in reason(output)


def test_foreign_prefix_denied_as_opus_without_header(gate):
    """Verify a non-plugin prefix is not stripped for the tier check.

    Mutation: stripping any prefix (not just the plugin's) before the
        tier lookup, so other:haiku passes as uncapped haiku.
    Oracle: other:haiku with no header is denied naming round: swarm,
        proving it is counted as Opus.
    """
    output = gate.gate_agent(agent_input('task', subagent_type='other:haiku'))
    assert decision(output) == 'deny'
    assert 'round: swarm' in reason(output)


def test_agent_scope_mixed_case_is_allowed(gate):
    """Verify a mixed-case prefixed type is read as the tier it names.

    Mutation: comparing subagent_type raw in is_uncapped, so
        Agent-Scope:Haiku is not read as the haiku tier, counts as Opus,
        and is denied for the missing header.
    Oracle: the mixed-case launch with no header is allowed.
    """
    output = gate.gate_agent(agent_input('task', subagent_type='Agent-Scope:Haiku'))
    assert output is None


def test_legacy_marker_lines_are_not_a_header(gate):
    """Verify the retired REVIEW-ROUND line syntax marks nothing.

    Mutation: keeping a whole-prompt regex for the old marker lines.
    Oracle: an Opus prompt opening with the old lines is denied as
        unmarked, not counted; an agent-scope:sonnet-high one passes twelve
        times.
    """
    prompt = 'REVIEW-ROUND: review\nREVIEW-COMPLEXITY: basic\nquoted documentation'
    assert 'round: swarm' in reason(gate.gate_agent(agent_input(prompt)))
    cheap = agent_input(prompt, subagent_type='agent-scope:sonnet-high')
    assert all(gate.gate_agent(cheap) is None for _ in range(12))


def test_header_matching_ignores_case_and_tag_spacing(gate):
    """Verify an uppercase header and a spaced open tag both count.

    Mutation: dropping IGNORECASE, requiring the literal <review-gate>
        so that <review-gate > slips through unmarked, or matching the
        tag as a prefix so that <review-gate-x> is a header.
    Oracle: three uppercase launches fill the cap and a spaced-tag
        launch is the denied fourth; a prompt opening with a different
        review-gate-x tag is plain text: denied as unmarked on Opus,
        allowed twelve times on sonnet-high.
    """
    upper = header().upper().replace('REVIEW THIS TEXT.', 'Review this text.')
    assert all(gate.gate_agent(agent_input(upper)) is None for _ in range(3))
    spaced = header().replace('<review-gate>', '<review-gate >', 1)
    output = gate.gate_agent(agent_input(spaced))
    assert decision(output) == 'deny'
    assert 'at most 3' in reason(output)
    other_tag = header().replace('<review-gate>', '<review-gate-x>', 1)
    assert 'round: swarm' in reason(gate.gate_agent(agent_input(other_tag)))
    cheap = agent_input(other_tag, subagent_type='agent-scope:sonnet-high')
    assert all(gate.gate_agent(cheap) is None for _ in range(12))


def test_header_line_ends(gate):
    """Verify the closing tag needs a line end, not a blank line, after it.

    Mutation: requiring a blank line after the header, rejecting a CRLF
        line end, or accepting text on the closing tag's own line.
    Oracle: a header directly above the brief and a CRLF header allow;
        text after the closing tag on its line denies.
    """
    assert gate.gate_agent(agent_input(header().replace('\n\n', '\n'))) is None
    assert gate.gate_agent(agent_input(header().replace('\n', '\r\n'))) is None
    same_line = header().replace('</review-gate>\n\n', '</review-gate> ')
    assert 'not closed' in reason(gate.gate_agent(agent_input(same_line)))


@pytest.mark.parametrize(
    ('prompt', 'message'),
    [
        ('<review-gate>\nround: review', 'not closed'),
        ('<review-gate>\n</review-gate>', 'round must be'),
        ('<review-gate>\nopus-cap: 3\n</review-gate>', 'round must be'),
        ('<review-gate>\nround: other\n</review-gate>', 'round must be'),
        ('<review-gate>\nround review\n</review-gate>', 'invalid review-gate header'),
        ('<review-gate>\nround: review\nextra: x\n</review-gate>', 'unknown'),
        ('<review-gate>\nround: review\nround: verify\n</review-gate>', 'duplicate'),
        (
            (
                '<review-gate>\nround: review\nopus-cap: 3\n'
                'opus-cap: 6\n</review-gate>'
            ),
            'duplicate',
        ),
        (
            '<review-gate>\nround: review\nopus-cap: 4\n</review-gate>',
            'invalid opus-cap',
        ),
        ])
def test_malformed_headers_are_denied(gate, prompt, message):
    """Verify each malformed header form is denied with a specific reason.

    Mutation: accepting ambiguous or incomplete header syntax, or
        collapsing the reasons into one generic message.
    Oracle: the named fragment appears in the deny reason.
    """
    output = gate.gate_agent(agent_input(prompt))
    assert decision(output) == 'deny'
    assert message in reason(output)


def test_invalid_line_reason_is_bounded(gate):
    """Verify a huge malformed header line is not echoed whole.

    Mutation: interpolating the offending line without a bound, which
        returns it to the model and writes it to the log verbatim.
    Oracle: a 200,000-character header line yields a reason under 200
        characters and a log line under 1,000.
    """
    prompt = '<review-gate>\n' + 'z' * 200_000 + '\n</review-gate>\nx'
    output = gate.gate_agent(agent_input(prompt))
    assert 'invalid review-gate header line' in reason(output)
    assert len(reason(output)) < 200
    assert len((gate.STATE_HOME / 'gate.jsonl').read_text().splitlines()[-1]) < 1000


@pytest.mark.parametrize(
    'kwargs',
    [
        {'model': 'sonnet'},
        {'subagent_type': 'Explore'},
        {'prompt': header('synthesize', 'huge')},
        ])
def test_bad_header_values_are_denied_on_every_marked_agent(gate, kwargs):
    """Verify header validation runs before the uncapped short-circuit.

    Mutation: returning allow for haiku, sonnet, Explore, or synthesize
        before validating the header.
    Oracle: a sonnet agent and an Explore agent with opus-cap huge,
        and a synthesize agent with opus-cap huge, are all denied; a
        sonnet agent with a duplicate round is denied.
    """
    payload = agent_input(kwargs.pop('prompt', header(opus_cap='huge')), **kwargs)
    output = gate.gate_agent(payload)
    assert decision(output) == 'deny'
    assert 'invalid opus-cap' in reason(output)
    duplicate = '<review-gate>\nround: review\nround: verify\n</review-gate>\nx'
    assert decision(gate.gate_agent(agent_input(duplicate, model='sonnet'))) == 'deny'


def test_opus_review_requires_opus_cap(gate):
    """Verify a marked Opus review agent without an opus-cap is denied.

    Mutation: defaulting an absent opus-cap to 3.
    Oracle: the deny reason names the requirement.
    """
    output = gate.gate_agent(agent_input(header(opus_cap=None)))
    assert decision(output) == 'deny'
    assert 'require opus-cap' in reason(output)


# --- Caps ---


@pytest.mark.parametrize(
    ('opus_cap', 'cap'),
    [('3', 3), ('6', 6), ('9', 9)])
def test_each_opus_cap_enforces_its_maximum(gate, opus_cap, cap):
    """Verify each opus-cap admits exactly its cap of Opus agents per round.

    Mutation: an off-by-one in the comparison, or a wrong CAPS entry.
    Oracle: cap launches allow, launch cap+1 is denied naming the cap.
    """
    prompt = header(opus_cap=opus_cap)
    assert all(gate.gate_agent(agent_input(prompt)) is None for _ in range(cap))
    output = gate.gate_agent(agent_input(prompt))
    assert decision(output) == 'deny'
    assert f'at most {cap}' in reason(output)


def test_review_and_verify_have_independent_caps(gate):
    """Verify the review and verify rounds each have their own counter.

    Mutation: sharing one counter between rounds.
    Oracle: three slots exist in each round at opus-cap 3 and the fourth of
        either is denied.
    """
    assert all(gate.gate_agent(agent_input(header('review'))) is None for _ in range(3))
    assert all(gate.gate_agent(agent_input(header('verify'))) is None for _ in range(3))
    assert decision(gate.gate_agent(agent_input(header('review')))) == 'deny'
    assert decision(gate.gate_agent(agent_input(header('verify')))) == 'deny'


def test_verify_round_needs_no_prior_review_round(gate):
    """Verify the rounds are types, not a sequence.

    Mutation: denying a verify launch while the review counter is zero.
    Oracle: a verify agent as the first launch of the cycle allows.
    """
    assert gate.gate_agent(agent_input(header('verify'))) is None


def test_denied_launch_does_not_consume_a_slot(gate):
    """Verify a refused reservation leaves the counter where it was.

    Mutation: incrementing before the cap comparison.
    Oracle: after two over-cap denials the log still shows slot 4 of 3
        and a verify-round launch gets its full three slots.
    """
    assert decision(exhaust_default_cap(gate)) == 'deny'
    assert decision(gate.gate_agent(agent_input(header()))) == 'deny'
    assert last_log(gate)['round_n'] == 4
    assert all(gate.gate_agent(agent_input(header('verify'))) is None for _ in range(3))


@pytest.mark.parametrize(
    'tool_overrides',
    [
        {'model': 'haiku'},
        {'model': 'sonnet'},
        {'model': 'Sonnet'},
        {'subagent_type': 'Explore'},
        {'subagent_type': 'agent-scope:sonnet-medium'},
        {'subagent_type': 'agent-scope:sonnet-high'},
        {'subagent_type': 'agent-scope:haiku'},
        ])
def test_cheap_marked_agents_are_uncapped_in_every_round(gate, tool_overrides):
    """Verify cheap models, cheap tiers, and Explore never take an Opus slot.

    Mutation: charging cheap agents to the Opus caps or the synthesize
        cap, matching the model alias case-sensitively, omitting a cheap
        tier name, or dropping the uncapped log marker.
    Oracle: twelve marked calls without an opus-cap, three per round, all
        allow, the last logs scope uncapped, and no state is written.
    """
    kwargs = {'model': None, 'subagent_type': 'agent-scope:opus-high', **tool_overrides}
    for index in range(12):
        round_name = gate.ROUNDS[index % len(gate.ROUNDS)]
        assert gate.gate_agent(agent_input(header(round_name, None), **kwargs)) is None
    assert last_log(gate)['scope'] == 'uncapped'
    assert not state_file(gate).exists()


@pytest.mark.parametrize(
    'kwargs',
    [
        {'model': 'opus'},
        {'model': 'notsonnet'},
        {'model': 'opus', 'subagent_type': 'Explore'},
        {'subagent_type': 'agent-scope:opus-medium'},
        {'subagent_type': 'Plan'},
        ])
def test_other_models_are_counted_as_opus(gate, kwargs):
    """Verify Opus tiers, other named types, and stray aliases take a slot.

    Mutation: substring matching on the alias ('sonnet' in 'notsonnet'),
        treating an explicit opus as outside the cap, exempting Explore
        by type alone when the pin hook strips its opus alias and it then
        runs on the main-loop Opus, or exempting an Opus tier or an
        inheriting named type such as Plan.
    Oracle: the fourth launch at opus-cap 3 is denied.
    """
    payload = agent_input(header(), **kwargs)
    assert all(gate.gate_agent(payload) is None for _ in range(3))
    assert decision(gate.gate_agent(payload)) == 'deny'


def test_synthesize_needs_no_opus_cap_and_holds_two_opus_per_cycle(gate):
    """Verify the synthesize round has its own cap of two and no opus-cap.

    Mutation: requiring an opus-cap on synthesize, leaving it uncapped, an
        off-by-one against SYNTHESIZE_CAP, or charging it to the review
        counter.
    Oracle: two Opus synthesize launches without an opus-cap allow, the
        third is denied naming 2, and the review round still has its
        three slots at opus-cap 3 afterwards.
    """
    payload = agent_input(header('synthesize', None))
    assert all(gate.gate_agent(payload) is None for _ in range(2))
    output = gate.gate_agent(payload)
    assert decision(output) == 'deny'
    assert 'at most 2' in reason(output)
    assert all(gate.gate_agent(agent_input(header())) is None for _ in range(3))


def test_synthesize_never_fixes_opus_cap_but_must_match_it(gate):
    """Verify a synthesize agent's opus-cap is compared, never fixed.

    Mutation: letting a synthesize agent fix the cycle's opus-cap, or
        skipping the mismatch comparison on the synthesize round.
    Oracle: a synthesize agent declaring 9 runs first and the review
        round still caps at 3 under opus-cap 3; a later synthesize agent
        declaring 6 in that cycle is denied naming 3, and one declaring
        nothing allows.
    """
    assert gate.gate_agent(agent_input(header('synthesize', '9'))) is None
    assert decision(exhaust_default_cap(gate)) == 'deny'
    output = gate.gate_agent(agent_input(header('synthesize', '6')))
    assert decision(output) == 'deny'
    assert 'declared opus-cap 3' in reason(output)
    assert gate.gate_agent(agent_input(header('synthesize', None))) is None


def test_xhigh_synthesize_cannot_escape_a_cycle_at_3(gate):
    """Verify an opus-xhigh synthesizer cannot escape a cycle fixed at 3.

    Mutation: skipping the mismatch comparison for synthesize, which
        would let the header alone re-declare the cap and its two seats
        in any cycle.
    Oracle: after one review agent at opus-cap 3, an opus-xhigh synthesize
        launch declaring 9 is denied as a mismatch naming 3, not on the
        seat that 3 lacks.
    """
    assert gate.gate_agent(agent_input(header())) is None
    output = gate.gate_agent(xhigh('synthesize', '9'))
    assert decision(output) == 'deny'
    assert 'declared opus-cap 3' in reason(output)
    assert 'seats no' not in reason(output)


@pytest.mark.parametrize('model', ['sonnet', 'haiku'])
@pytest.mark.parametrize('tier', [
    'agent-scope:opus-medium', 'agent-scope:opus-high',
    'agent-scope:opus-xhigh', 'agent-scope:fable-xhigh'])
def test_a_cheap_model_keeps_any_capped_tier_uncapped(gate, tier, model):
    """Verify the uncapped check runs before the derive presence rule.

    Mutation: moving the presence rule above the uncapped check, which
        would deny a launch that runs on sonnet or haiku for naming no
        kind; or exempting only the Opus tiers, so an explicit cheap
        model beside fable-xhigh is charged a slot.
    Oracle: four capped tiers at opus-cap 3, each with an explicit cheap
        model and no derive, allow twelve times over, log scope uncapped,
        and write no state.
    """
    payload = agent_input(header(), subagent_type=tier, model=model)
    assert all(gate.gate_agent(payload) is None for _ in range(12))
    assert last_log(gate)['scope'] == 'uncapped'
    assert not state_file(gate).exists()


@pytest.mark.parametrize('opus_cap', ['3', '6', '9'])
@pytest.mark.parametrize('round_name', ['review', 'verify', 'synthesize', 'swarm'])
@pytest.mark.parametrize(
    'tier', ['agent-scope:opus-xhigh', 'agent-scope:fable-xhigh'])
def test_a_deriving_tier_needs_a_kind_in_every_round(
    gate, tier, round_name, opus_cap):
    """Verify each deriving tier is denied without a kind whatever the cap.

    Mutation: dropping the presence rule, applying it to the counted
        rounds only, applying it to opus-xhigh alone so fable-xhigh
        seats with no kind, or letting opus-cap 9 stand in for the kind.
    Oracle: the launch is denied naming derive: <kind> and the six kinds,
        and takes no slot: an opus-high launch at the same cap then logs
        round_n 1.
    """
    payload = agent_input(header(round_name, opus_cap), subagent_type=tier)
    output = gate.gate_agent(payload)
    assert decision(output) == 'deny'
    assert 'needs derive: <kind>' in reason(output)
    assert 'joint-behavior' in reason(output)
    assert gate.gate_agent(agent_input(header(round_name, opus_cap))) is None
    assert last_log(gate)['round_n'] == 1


def test_denied_xhigh_takes_no_slot(gate):
    """Verify the derive presence rule runs before the reservation.

    Mutation: reserving the slot and then denying on the missing kind,
        which at opus-cap 3 would still deny on the seat but write the
        normalized state file.
    Oracle: after an opus-xhigh denial for no kind under opus-cap 3, no
        state file exists and three opus-high launches at 3 still allow.
    """
    output = gate.gate_agent(
        agent_input(header(), subagent_type='agent-scope:opus-xhigh'))
    assert decision(output) == 'deny'
    assert not state_file(gate).exists()
    assert all(gate.gate_agent(agent_input(header())) is None for _ in range(3))


@pytest.mark.parametrize(
    'subagent_type', ['agent-scope:opus-xhigh', 'agent-scope:sonnet-high'])
def test_unknown_derive_kind_is_denied_on_any_marked_agent(gate, subagent_type):
    """Verify derive takes one of six kinds, checked before the uncapped exit.

    Mutation: accepting any token as a kind, or validating the value
        only on Opus-tier launches so a cheap agent carries junk metadata.
    Oracle: derive: importance is denied on opus-xhigh and on sonnet-high
        alike, naming the six kinds and opus-high; no state is written.
    """
    payload = agent_input(header(derive='importance'), subagent_type=subagent_type)
    output = gate.gate_agent(payload)
    assert decision(output) == 'deny'
    assert 'invalid derive: importance' in reason(output)
    assert 'formula, bound, proof, equivalence, interleaving, joint-behavior' in (
        reason(output))
    assert 'use agent-scope:opus-high' in reason(output)
    assert not state_file(gate).exists()


@pytest.mark.parametrize(
    'subagent_type', ['agent-scope:opus-high', 'agent-scope:opus-medium', 'Plan'])
def test_derive_on_another_tier_is_denied_and_takes_no_slot(gate, subagent_type):
    """Verify derive belongs on opus-xhigh alone and cannot be banked.

    Mutation: ignoring the field on other tiers, so a habit of writing
        derive into every header costs nothing and hides the claim.
    Oracle: an Opus launch below xhigh declaring derive: proof is denied
        naming its type and the field; three plain opus-high launches
        then fit.
    """
    payload = agent_input(header(derive='proof'), subagent_type=subagent_type)
    output = gate.gate_agent(payload)
    assert decision(output) == 'deny'
    assert 'derive belongs on a deriving tier' in reason(output)
    assert f'{subagent_type} declared derive: proof' in reason(output)
    assert all(gate.gate_agent(agent_input(header())) is None for _ in range(3))


def test_opus_xhigh_is_allowed_and_counted(gate):
    """Verify opus-xhigh with a kind passes at opus-cap 6 and takes a slot.

    Mutation: still requiring opus-cap 9 for the tier, or exempting xhigh
        from the Opus cap.
    Oracle: one opus-xhigh review launch at 6 and five opus-high launches
        allow; the seventh capped launch is denied naming 6, so the xhigh
        launch counted as an Opus slot.
    """
    assert decision(gate.gate_agent(xhigh())) is None
    high = agent_input(header(opus_cap='6'))
    assert all(gate.gate_agent(high) is None for _ in range(5))
    output = gate.gate_agent(high)
    assert decision(output) == 'deny'
    assert 'at most 6' in reason(output)


def test_second_xhigh_at_cap_6_is_denied_and_takes_no_slot(gate):
    """Verify a cycle at 6 admits one opus-xhigh and a refusal costs nothing.

    Mutation: dropping the seat check, moving the Opus counter before the
        seat refusal, or reporting the Opus position instead of the seat
        number in the denial.
    Oracle: after one opus-xhigh and one opus-high review launch at 6 the
        second opus-xhigh is denied naming opus-high and seat #2, not #3;
        four more opus-high launches then allow and the next is denied,
        so the refused xhigh took no Opus slot.
    """
    assert decision(gate.gate_agent(xhigh())) is None
    high = agent_input(header(opus_cap='6'))
    assert gate.gate_agent(high) is None
    output = gate.gate_agent(xhigh())
    assert decision(output) == 'deny'
    assert 'opus-high' in reason(output)
    assert 'holds 1 derive seat' in reason(output)
    assert '#2' in reason(output)
    assert all(gate.gate_agent(high) is None for _ in range(4))
    assert decision(gate.gate_agent(high)) == 'deny'


def test_cap_3_seats_no_xhigh_and_fixes_nothing(gate):
    """Verify cap 3 seats no opus-xhigh and the refusal fixes nothing.

    Mutation: a nonzero entry for 3 in XHIGH_SEATS, the zero-seat denial
        falling through to the count wording, or a seat refusal that
        still fixes the declared cap.
    Oracle: an opus-xhigh review launch declaring 3 with a kind is denied
        naming no seat and opus-cap 6 or 9, logging seat_n 1 and
        seat_cap 0; a review launch at 6 in the same cycle then allows,
        so 3 was never fixed.
    """
    output = gate.gate_agent(xhigh('review', '3'))
    assert decision(output) == 'deny'
    assert 'a cycle at opus-cap 3 holds no derive seat' in reason(output)
    assert 'opus-cap 6 or 9' in reason(output)
    line = last_log(gate)
    assert (line['refusal'], line['seat_n'], line['seat_cap']) == ('seat', 1, 0)
    assert gate.gate_agent(agent_input(header(opus_cap='6'))) is None


@pytest.mark.parametrize(('opus_cap', 'seats'), [('6', 1), ('9', 2)])
def test_each_opus_cap_sets_its_xhigh_seats(gate, opus_cap, seats):
    """Verify the seat count reads off the cap: one at 6, two at 9.

    Mutation: a wrong entry in XHIGH_SEATS, a seat count read off the
        round instead of the cycle, or a seat message that miscounts.
    Oracle: `seats` opus-xhigh launches across the review and verify
        rounds allow, each message naming seat i/seats; the next is
        denied naming the cap, the count, and #seats+1.
    """
    for taken in range(1, seats + 1):
        round_name = 'review' if taken % 2 else 'verify'
        output = gate.gate_agent(xhigh(round_name, opus_cap))
        assert f'opus-xhigh seat {taken}/{seats}' in seat_message(output)
    output = gate.gate_agent(xhigh('verify', opus_cap))
    assert decision(output) == 'deny'
    assert f'a cycle at opus-cap {opus_cap} holds {seats} derive seat' in (
        reason(output))
    assert f'#{seats + 1}' in reason(output)


def test_seat_fields_are_logged_on_the_xhigh_lines(gate):
    """Verify seat_n and seat_cap are logged on every opus-xhigh decision.

    Mutation: logging the seat under round_n and round_cap, omitting the
        seat fields or the derive kind on the allowed line, or keeping
        round_n on a seat denial.
    Oracle: the allowed opus-xhigh line at 6 carries round_n 1, seat_n 1,
        seat_cap 1, and derive proof; the second's line carries refusal
        seat, seat_n 2, seat_cap 1, and no round_n.
    """
    assert decision(gate.gate_agent(xhigh())) is None
    line = last_log(gate)
    assert (line['round_n'], line['seat_n'], line['seat_cap']) == (1, 1, 1)
    assert line['derive'] == 'proof'
    assert decision(gate.gate_agent(xhigh())) == 'deny'
    line = last_log(gate)
    assert line['refusal'] == 'seat'
    assert (line['seat_n'], line['seat_cap']) == (2, 1)
    assert 'round_n' not in line


def test_seat_refusal_yields_to_a_full_opus_cap(gate):
    """Verify a full Opus cap is reported before a taken xhigh seat.

    Mutation: checking the seat before the cap, which would send the
        reader to opus-high when that tier has no room either.
    Oracle: with the seat taken and six capped slots used at 6, a further
        opus-xhigh launch is denied naming the cap of 6, not the seat.
    """
    assert decision(gate.gate_agent(xhigh())) is None
    high = agent_input(header(opus_cap='6'))
    assert all(gate.gate_agent(high) is None for _ in range(5))
    output = gate.gate_agent(xhigh())
    assert decision(output) == 'deny'
    assert 'at most 6' in reason(output)
    assert 'admits' not in reason(output)


def test_refused_xhigh_leaves_the_seat_free(gate):
    """Verify a mismatch-refused opus-xhigh does not take a seat.

    Mutation: checking the seat before the opus-cap mismatch, which would
        answer a wrong declaration with the seat wording; or storing every
        counter, so an untaken seat is written as 0.
    Oracle: after a review launch at opus-cap 3, an opus-xhigh review
        launch declaring 9 is denied naming the fixed 3, and the state
        file has no xhigh key.
    """
    assert gate.gate_agent(agent_input(header())) is None
    output = gate.gate_agent(xhigh('review', '9'))
    assert decision(output) == 'deny'
    assert 'declared opus-cap 3' in reason(output)
    cycle = next(iter(json.loads(state_file(gate).read_text())['cycles'].values()))
    assert 'xhigh' not in cycle


@pytest.mark.parametrize(
    'tier', ['agent-scope:opus-xhigh', 'agent-scope:fable-xhigh'])
def test_a_deriving_synthesize_needs_a_fixed_opus_cap(gate, tier):
    """Verify a deriving synthesizer waits for a fixed opus-cap.

    Mutation: dropping the unfixed rule, which would seat a deriving
        synthesize agent before any cap says how many seats the cycle
        has; or applying the rule to opus-xhigh alone.
    Oracle: a deriving synthesize launch first in a cycle is denied
        naming the fixing rounds and writes no seat; after one opus-high
        review launch at 6 the same launch allows as seat 1/1.
    """
    synthesize = agent_input(
        header('synthesize', None, 'proof'), subagent_type=tier)
    output = gate.gate_agent(synthesize)
    assert decision(output) == 'deny'
    assert 'review, verify, or swarm agent has fixed' in reason(output)
    assert stored_cycle(gate) == {
        'opus_cap': None, 'review': 0, 'verify': 0, 'swarm': 0, 'synthesize': 0}
    assert gate.gate_agent(agent_input(header(opus_cap='6'))) is None
    name = tier.split(':')[1]
    assert f'{name} seat 1/1' in seat_message(gate.gate_agent(synthesize))


def test_xhigh_seat_is_per_cycle_across_all_four_rounds(gate):
    """Verify the seats are one counter across all four rounds.

    Mutation: keying the seat on the round, which multiplies the
        allowance by four.
    Oracle: at opus-cap 9, opus-xhigh launches in review and swarm allow;
        a third in synthesize is denied as #3 of 2, and the state holds
        xhigh 2 with no per-round seat key.
    """
    assert decision(gate.gate_agent(xhigh('review', '9'))) is None
    assert decision(gate.gate_agent(xhigh('swarm', '9'))) is None
    output = gate.gate_agent(xhigh('synthesize', '9'))
    assert decision(output) == 'deny'
    assert 'holds 2 derive seats; this would be #3' in reason(output)
    cycle = next(iter(json.loads(state_file(gate).read_text())['cycles'].values()))
    assert cycle['xhigh'] == 2
    assert not any(key.endswith('-xhigh') for key in cycle)


def test_xhigh_seat_survives_a_damaged_counter(gate):
    """Verify a corrupted xhigh seat counter reads as empty, not as full.

    Mutation: treating a non-int seat value as taken, or crashing on it.
    Oracle: with the xhigh seat stored as a string, one opus-xhigh
        review launch allows and the second is denied.
    """
    assert decision(gate.gate_agent(xhigh())) is None
    path = state_file(gate)
    state = json.loads(path.read_text())
    cycle = next(iter(state['cycles'].values()))
    cycle['xhigh'] = 'one'
    path.write_text(json.dumps(state))
    assert decision(gate.gate_agent(xhigh())) is None
    assert decision(gate.gate_agent(xhigh())) == 'deny'


def test_allowed_xhigh_returns_a_seat_message(gate):
    """Verify an allowed opus-xhigh launch shows the user seat, kind, label.

    Mutation: returning silence on the allowed launch, adding a
        permission decision to the envelope, or dropping the kind or the
        label from the line.
    Oracle: the output is a systemMessage with no hookSpecificOutput,
        naming opus-xhigh seat 1/1, derive: proof, and the description
        reviewer; an opus-high launch still returns None.
    """
    output = gate.gate_agent(xhigh())
    assert set(output) == {'systemMessage'}
    message = seat_message(output)
    assert 'review-gate: opus-xhigh seat 1/1' in message
    assert 'derive: proof' in message
    assert message.endswith(' - reviewer')
    assert gate.gate_agent(agent_input(header(opus_cap='6'))) is None


def test_xhigh_with_no_cycle_key_is_allowed_and_announced(gate):
    """Verify an uncounted opus-xhigh launch still reaches the user.

    Mutation: returning silence on the no-turn path, so the one launch
        the gate cannot count is also the one the user never sees.
    Oracle: with no prompt_id and no transcript, the launch logs scope
        no-turn and the derive kind, and the message says uncounted.
    """
    output = gate.gate_agent(xhigh(prompt_id=None))
    assert 'opus-xhigh uncounted, no cycle key' in seat_message(output)
    line = last_log(gate)
    assert (line['scope'], line['derive']) == ('no-turn', 'proof')


def test_fable_is_always_denied(gate):
    """Verify a fable model is denied whether or not the prompt is marked.

    Mutation: applying the model ban only inside the review path, or
        matching the alias case-sensitively.
    Oracle: an unmarked fable Agent and a marked Fable one are denied.
    """
    output = gate.gate_agent(agent_input('ordinary task', model='fable'))
    assert decision(output) == 'deny'
    assert 'fable' in reason(output)
    assert decision(gate.gate_agent(agent_input(header(), model='Fable'))) == 'deny'


def test_opus_cap_is_fixed_by_the_first_counted_agent(gate):
    """Verify one opus-cap holds across both rounds for the whole cycle.

    Mutation: storing the opus-cap per round, letting a mismatch rewrite
        the stored value or take a slot, or logging the declared value
        without the fixed one.
    Oracle: after a review launch at 3, a verify launch at 6 is denied
        naming 3, a retry at 6 is still denied and logs fixed 3 with
        cap 3, and two more launches at 3 fit before the fourth is
        denied.
    """
    assert gate.gate_agent(agent_input(header('review', '3'))) is None
    output = gate.gate_agent(agent_input(header('verify', '6')))
    assert decision(output) == 'deny'
    assert 'declared opus-cap 3' in reason(output)
    retry = gate.gate_agent(agent_input(header('review', '6')))
    assert 'declared opus-cap 3' in reason(retry)
    line = last_log(gate)
    logged = (line['opus_cap'], line['fixed'], line['round_cap'])
    assert logged == ('6', '3', 3)
    default = agent_input(header('review', '3'))
    assert all(gate.gate_agent(default) is None for _ in range(2))
    assert decision(gate.gate_agent(default)) == 'deny'


# --- Cycle and session keys ---


def test_new_prompt_starts_a_new_review_cycle(gate):
    """Verify a new prompt_id resets the counters and the opus-cap.

    Mutation: keying the state by session alone.
    Oracle: three launches at 3 under turn-1, then a launch at 6 under
        turn-2 allows.
    """
    first = agent_input(header(opus_cap='3'), prompt_id='turn-1')
    assert all(gate.gate_agent(first) is None for _ in range(3))
    second = agent_input(header(opus_cap='6'), prompt_id='turn-2')
    assert gate.gate_agent(second) is None


def test_sessions_have_independent_state(gate):
    """Verify two sessions never share a counter.

    Mutation: writing one global state file.
    Oracle: each of two sessions gets its three slots in the same turn.
    """
    for session in ('one', 'two'):
        payload = agent_input(header(), session=session)
        assert all(gate.gate_agent(payload) is None for _ in range(3))


@pytest.mark.parametrize(
    'damage',
    [
        '{not json',
        '[1, 2, 3]',
        '{"cycles": []}',
        '{"cycles": {"turn-1": {"opus_cap": "3", "review": null, "verify": 0}}}',
        '{"cycles": {"turn-1": {"opus_cap": ["3"], "review": 0, "verify": 0}}}',
        '{"cycles": {"turn-1": "3"}}',
        '{"cycles": {"turn-1": {"opus_cap": "3", "review": -100, "verify": 0}}}',
        '{"cycles": {"turn-1": {"opus_cap": "3", "review": true, "verify": 0}}}',
        ])
def test_damaged_state_file_is_repaired_and_still_caps(gate, damage):
    """Verify a damaged state file reads as empty state and gating resumes.

    Mutation: letting json.loads, int(), or a dict lookup raise, which
        would fail open for the rest of the cycle and never rewrite the
        file; dropping the non-dict guard; or accepting a negative or
        boolean counter, which would hand out extra slots or eat one.
    Oracle: after one launch and the damage, the next launch is slot 1
        again and the fourth is denied.
    """
    gate.gate_agent(agent_input(header()))
    state_file(gate).write_text(damage)
    assert gate.gate_agent(agent_input(header())) is None
    assert last_log(gate)['round_n'] == 1
    assert all(gate.gate_agent(agent_input(header())) is None for _ in range(2))
    assert decision(gate.gate_agent(agent_input(header()))) == 'deny'


def test_stale_opus_cap_value_is_refixed(gate):
    """Verify an unknown stored opus-cap is replaced, not looked up.

    Mutation: deleting the re-fix branch so CAPS[fixed] raises KeyError.
    Oracle: with a retired word stored as the opus_cap under the current
        turn, three launches at 3 allow and the fourth is denied naming 3.
    """
    state_file(gate).write_text(
        '{"cycles": {"turn-1": {"opus_cap": "basic", "review": 0, "verify": 0}}}')
    output = exhaust_default_cap(gate)
    assert decision(output) == 'deny'
    assert 'at most 3' in reason(output)


def test_state_file_holds_only_recent_cycles(gate):
    """Verify the state file is rewritten from scratch and pruned to 8 cycles.

    Mutation: dropping state.clear() so keys from an older schema linger,
        or dropping the prune so the file grows for the life of the
        session.
    Oracle: a file seeded with old keys holds exactly one cycle entry
        after one launch; after launches under nine distinct prompt ids
        the oldest is gone and the newest eight remain in order.
    """
    state_file(gate).write_text('{"turn": "old", "costly": 21, "all": 21}')
    assert gate.gate_agent(agent_input(header())) is None
    state = json.loads(state_file(gate).read_text())
    assert state == {
        'cycles': {
            'turn-1': {
                'opus_cap': '3',
                'review': 1,
                'verify': 0,
                'swarm': 0,
                'synthesize': 0,
                },
            },
        }
    for index in range(2, 10):
        assert gate.gate_agent(agent_input(header(), prompt_id=f'turn-{index}')) is None
    cycles = json.loads(state_file(gate).read_text())['cycles']
    assert list(cycles) == [f'turn-{index}' for index in range(2, 10)]


def test_interleaved_cycles_keep_their_own_counters(gate):
    """Verify launches from two cycles may interleave without a reset.

    Mutation: storing one cycle and clearing it whenever the key changes,
        which lets t1, t1, t1, t2, t1 pass and neither cycle ever cap.
    Oracle: three t1 launches, one t2, then a fourth t1 is denied; two
        more t2 allow and the fourth t2 is denied.
    """
    t1 = agent_input(header(), prompt_id='t1')
    t2 = agent_input(header(), prompt_id='t2')
    assert all(gate.gate_agent(t1) is None for _ in range(3))
    assert gate.gate_agent(t2) is None
    assert decision(gate.gate_agent(t1)) == 'deny'
    assert all(gate.gate_agent(t2) is None for _ in range(2))
    assert decision(gate.gate_agent(t2)) == 'deny'


@pytest.mark.parametrize(
    ('session', 'name'),
    [
        ('../../evil', '.._.._evil.json'),
        ('sess\u4e2d\u6587', 'sess__.json'),
        ('', 'nosession.json'),
        ('x' * 300, 'x' * 120 + '.json'),
        ])
def test_session_ids_are_sanitized_into_state_home(gate, session, name):
    """Verify every session id maps to an ASCII file inside STATE_HOME.

    Mutation: using the raw id as a path, letting Unicode word characters
        through, or dropping the empty-id fallback or the length cap.
    Oracle: the expected file name exists under STATE_HOME and nothing
        lands two directories up.
    """
    assert gate.gate_agent(agent_input(header(), session=session)) is None
    assert (gate.STATE_HOME / name).exists()
    assert not (gate.STATE_HOME.parent.parent / 'evil.json').exists()


def test_fallback_uses_the_last_user_message_not_a_tool_result(gate, tmp_path):
    """Verify the transcript fallback picks the last real user message.

    Mutation: scanning forward and returning the first user line,
        counting a tool_result row as a user message, crashing on a row
        that is valid JSON but not an object, or letting a NUL in the
        path raise ValueError past the OSError guard.
    Oracle: u1, an assistant row, a scalar row, an array row, a
        tool_result u2 -> u1; appending u3 -> u3; a missing file and a
        NUL path -> None; a prompt_id no system record carries wins over
        the transcript.
    """
    path = tmp_path / 'transcript.jsonl'
    lines = [
        transcript_line('user', 'u1', 'hi'),
        transcript_line('assistant', 'a1', []),
        '"text"',
        '[1, 2]',
        transcript_line('user', 'u2', [{'type': 'tool_result', 'content': 'x'}]),
        ]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert gate.turn_key({'transcript_path': str(path)}) == 'u1'
    with path.open('a', encoding='utf-8') as stream:
        stream.write(transcript_line('user', 'u3', 'next') + '\n')
    assert gate.turn_key({'transcript_path': str(path)}) == 'u3'
    assert gate.turn_key({'transcript_path': str(tmp_path / 'none.jsonl')}) is None
    assert gate.turn_key({'transcript_path': 'a\x00b'}) is None
    assert gate.turn_key({'prompt_id': 'p-7', 'transcript_path': str(path)}) == 'p-7'


def test_task_notification_prompt_id_resolves_to_the_human_prompt(gate, tmp_path):
    """Verify a prompt_id minted for a task notification keys the human prompt.

    Mutation: keying on the payload's prompt_id whenever it is present,
        or keying on the transcript's human record whenever the two
        differ, which moves a launch whose human record has not reached
        the file onto the previous prompt.
    Oracle: human p1, tool output, a task-notification stamped p2 ->
        payload p2 keys p1 and payload p1 keys p1; a payload p9 that no
        record carries stays p9; a new human p3 keys p3.
    """
    path = tmp_path / 'transcript.jsonl'
    lines = [
        transcript_line(
            'user', 'u1', 'review this', origin='human', prompt_id='p1'),
        transcript_line('assistant', 'a1', []),
        transcript_line('user', 'u2', [{'type': 'tool_result', 'content': 'x'}]),
        transcript_line(
            'user', 'u3', '<task-notification/>',
            origin='task-notification', prompt_id='p2'),
        ]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert gate.turn_key({'prompt_id': 'p2', 'transcript_path': str(path)}) == 'p1'
    assert gate.turn_key({'prompt_id': 'p1', 'transcript_path': str(path)}) == 'p1'
    assert gate.turn_key({'prompt_id': 'p9', 'transcript_path': str(path)}) == 'p9'
    with path.open('a', encoding='utf-8') as stream:
        stream.write(
            transcript_line('user', 'u4', 'now fix it', origin='human', prompt_id='p3')
            + '\n')
    assert gate.turn_key({'prompt_id': 'p3', 'transcript_path': str(path)}) == 'p3'


def test_task_notification_turn_does_not_reset_the_cap(gate, tmp_path):
    """Verify the Opus cap survives a background task re-entering the turn.

    Mutation: a new payload prompt_id opening a new cycle while the
        transcript shows it stamped on a task-notification record.
    Oracle: three Opus launches under the human prompt p1, then a
        task-notification stamped p2 -> a fourth launch carrying p2 is
        denied as #4; a new human prompt p3 is then allowed.
    """
    path = tmp_path / 'transcript.jsonl'
    path.write_text(
        transcript_line('user', 'u1', 'review', origin='human', prompt_id='p1')
        + '\n',
        encoding='utf-8')
    first = agent_input(header(), prompt_id='p1', transcript_path=path)
    assert all(gate.gate_agent(first) is None for _ in range(3))
    with path.open('a', encoding='utf-8') as stream:
        stream.write(
            transcript_line(
                'user', 'u2', '<task-notification/>',
                origin='task-notification', prompt_id='p2')
            + '\n')
    fourth = agent_input(header(), prompt_id='p2', transcript_path=path)
    assert 'this would be #4' in reason(gate.gate_agent(fourth))
    with path.open('a', encoding='utf-8') as stream:
        stream.write(
            transcript_line('user', 'u3', 'next', origin='human', prompt_id='p3')
            + '\n')
    third = agent_input(header(), prompt_id='p3', transcript_path=path)
    assert gate.gate_agent(third) is None


def test_fallback_skips_system_and_meta_records(gate, tmp_path):
    """Verify the no-prompt_id path opens no cycle on a system or meta record.

    Mutation: returning the first user record that is not a tool_result,
        which keys the cycle on a task notification or a command
        expansion, or stopping at a legacy record when a human record
        stands behind it.
    Oracle: legacy u1, a meta expansion u2, a task-notification u3 ->
        u1; a human record stamped p0 prepended -> p0.
    """
    path = tmp_path / 'transcript.jsonl'
    lines = [
        transcript_line('user', 'u1', 'hi'),
        transcript_line(
            'user', 'u2', '<command-message>x</command-message>',
            meta=True, prompt_id='p1'),
        transcript_line(
            'user', 'u3', '<task-notification/>',
            origin='task-notification', prompt_id='p2'),
        ]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert gate.turn_key({'transcript_path': str(path)}) == 'u1'
    human = transcript_line('user', 'u0', 'first', origin='human', prompt_id='p0')
    path.write_text('\n'.join([human, *lines]) + '\n', encoding='utf-8')
    assert gate.turn_key({'transcript_path': str(path)}) == 'p0'


def test_fallback_scans_past_a_large_tail(gate, tmp_path):
    """Verify the backward scan is not limited to the last chunk.

    Mutation: reading only the final 64 KiB, or dropping the line split
        across a chunk boundary.
    Oracle: the user uuid on the first line survives 400 KiB of
        assistant and tool output with no trailing newline.
    """
    transcript = tmp_path / 'large.jsonl'
    lines = [transcript_line('user', 'real-user', 'review')]
    lines.extend(transcript_line('assistant', f'a-{i}', 'x' * 4000) for i in range(100))
    lines.append(transcript_line('user', 'tool-result', [{'type': 'tool_result'}]))
    transcript.write_text('\n'.join(lines), encoding='utf-8')
    payload = agent_input(header(), prompt_id=None, transcript_path=transcript)
    assert gate.turn_key(payload) == 'real-user'


def test_large_transcript_growth_does_not_reset_the_cap(gate, tmp_path):
    """Verify tool output appended mid-cycle keeps the same turn key.

    Mutation: returning no-turn once the user line leaves the tail chunk.
    Oracle: after 400 KiB of appended assistant rows the fourth
        launch at opus-cap 3 is still denied.
    """
    transcript = tmp_path / 'growing.jsonl'
    transcript.write_text(transcript_line('user', 'real-user', 'review'))
    payload = agent_input(header(), prompt_id=None, transcript_path=transcript)
    assert all(gate.gate_agent(payload) is None for _ in range(3))
    with transcript.open('a', encoding='utf-8') as stream:
        for i in range(100):
            stream.write('\n' + transcript_line('assistant', str(i), 'x' * 4000))
    assert decision(gate.gate_agent(payload)) == 'deny'


def test_unresolved_cycle_key_allows_and_logs(gate):
    """Verify a launch whose cycle cannot be keyed is allowed and marked.

    Mutation: keying such launches on a constant, which caps the whole
        session for its life, or letting the missing key raise.
    Oracle: twelve Opus launches with no prompt_id and no transcript all
        allow and the last logs scope no-turn with the opus-cap.
    """
    payload = agent_input(header(), prompt_id=None)
    assert all(gate.gate_agent(payload) is None for _ in range(12))
    line = last_log(gate)
    assert (line['scope'], line['opus_cap']) == ('no-turn', '3')


# --- Locking ---


def test_parallel_calls_cannot_race_past_cap(gate):
    """Verify concurrent launches serialize on the state file lock.

    Mutation: reading and writing the state without a lock.
    Oracle: of twelve simultaneous launches at opus-cap 3 exactly three allow.
    """
    payload = agent_input(header())
    with ThreadPoolExecutor(max_workers=12) as executor:
        outputs = list(executor.map(lambda _: gate.gate_agent(payload), range(12)))
    assert sum(output is None for output in outputs) == 3
    assert sum(decision(output) == 'deny' for output in outputs) == 9


def test_windows_lock_path_round_trips(gate, monkeypatch):
    """Verify session_state locks through msvcrt when fcntl is absent.

    Mutation: calling fcntl unconditionally, or unlocking a different
        byte than the one locked.
    Oracle: with fcntl None and a recording msvcrt stub, state persists
        across two opens and the stub saw lock, unlock, lock, unlock at
        offset 0 each time.
    """
    calls = []

    def locking(fd, mode, nbytes):
        calls.append((mode, os.lseek(fd, 0, os.SEEK_CUR), nbytes))

    stub = types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=0, locking=locking)
    monkeypatch.setattr(gate, 'fcntl', None)
    monkeypatch.setattr(gate, 'msvcrt', stub, raising=False)
    with gate.session_state('win') as state:
        state['probe'] = 7
    with gate.session_state('win') as state:
        assert state['probe'] == 7
    assert calls == [(1, 0, 1), (0, 0, 1), (1, 0, 1), (0, 0, 1)]


# --- Log and CLI ---


def test_log_lines_carry_the_documented_fields(gate):
    """Verify allow and deny lines log every field the guide names.

    Mutation: renaming or dropping a logged key, or logging the wrong
        value under label or agent_type.
    Oracle: an unmarked agent-scope:sonnet-high launch logs scope
        not-review-marked with the description and tier type; a swarm
        launch logs its round and slot 1 of 6; a cap denial logs decision,
        turn, round, opus_cap, round_n 7 and round_cap 6 with the exact
        values.
    """
    assert gate.gate_agent(
        agent_input('unmarked', subagent_type='agent-scope:sonnet-high')) is None
    line = last_log(gate)
    assert (line['scope'], line['label'], line['agent_type']) == (
        'not-review-marked', 'reviewer', 'agent-scope:sonnet-high')
    assert gate.gate_agent(agent_input(header('swarm', '6'))) is None
    line = last_log(gate)
    assert (line['round'], line['round_n'], line['round_cap']) == ('swarm', 1, 6)
    for _ in range(7):
        gate.gate_agent(agent_input(header('verify', '6'), model='opus'))
    line = last_log(gate)
    assert {'ts', 'tool', 'session', 'model', 'agent_type', 'label'} <= set(line)
    expected = {
        'decision': 'deny',
        'turn': 'turn-1',
        'round': 'verify',
        'opus_cap': '6',
        'round_n': 7,
        'round_cap': 6,
        'model': 'opus',
        'label': 'reviewer',
        }
    assert {key: line[key] for key in expected} == expected
    assert line['reason'].startswith('review-gate: opus-cap 6 allows at most 6')


def test_log_write_failure_never_blocks_a_launch(gate, monkeypatch):
    """Verify an unwritable log leaves every decision intact.

    Mutation: letting the OSError out of log_event.
    Oracle: with LOG_PATH pointing at a directory, three launches allow
        and the fourth is still denied.
    """
    monkeypatch.setattr(gate, 'LOG_PATH', gate.STATE_HOME)
    assert decision(exhaust_default_cap(gate)) == 'deny'


def test_main_fails_open_on_an_internal_error(gate, monkeypatch, capsys):
    """Verify a crash inside the decision is logged and the launch continues.

    Mutation: letting the exception propagate (non-zero exit, no JSON).
    Oracle: main() prints nothing and the last log line has decision
        error with the exception text.
    """
    def explode(_hook_input):
        raise RuntimeError('boom')

    monkeypatch.setattr(gate, 'gate_agent', explode)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(agent_input(header()))))
    gate.main()
    assert not capsys.readouterr().out
    line = last_log(gate)
    assert line['decision'] == 'error'
    assert 'boom' in line['reason']


@pytest.mark.parametrize(
    'interpreter',
    list(INTERPRETERS.values()),
    ids=[os.path.basename(path) for path in INTERPRETERS])
def test_cli_end_to_end(tmp_path, interpreter):
    """Verify the hook as Claude Code runs it, under every Python present.

    Mutation: dispatching on the wrong payload key, printing on a plain
        allow, silence on an allowed opus-xhigh, or a 3.11-plus-only
        construct that crashes under an older python3.
    Oracle: three allows print nothing, the fourth prints the PreToolUse
        deny JSON, an opus-xhigh launch in a new cycle prints the seat
        message; another tool, bad JSON, and a list payload print
        nothing; a non-dict tool_input names no tier and prints a deny;
        every run exits 0 with empty stderr under a minimal environment.
    """
    env = {'PATH': os.environ.get('PATH', ''), 'REVIEW_GATE_HOME': str(tmp_path)}
    if 'SYSTEMROOT' in os.environ:
        env['SYSTEMROOT'] = os.environ['SYSTEMROOT']

    def run(payload):
        proc = subprocess.run(
            [interpreter, str(HOOK_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            check=False)
        assert proc.returncode == 0, proc.stderr
        assert not proc.stderr
        return proc.stdout

    marked = json.dumps(agent_input(header()))
    assert all(not run(marked) for _ in range(3))
    output = json.loads(run(marked))['hookSpecificOutput']
    assert output['hookEventName'] == 'PreToolUse'
    assert output['permissionDecision'] == 'deny'
    other_tool = {'session_id': 's', 'tool_name': 'Bash', 'tool_input': {}}
    assert not run(json.dumps(other_tool))
    assert not run('{not json')
    assert not run('[1, 2]')
    last = json.loads((tmp_path / 'gate.jsonl').read_text().splitlines()[-1])
    assert last['decision'] == 'deny'
    odd_input = {'session_id': 's', 'tool_name': 'Agent', 'tool_input': 'text'}
    odd = json.loads(run(json.dumps(odd_input)))['hookSpecificOutput']
    assert 'name the tier' in odd['permissionDecisionReason']
    seated = json.dumps(xhigh(prompt_id='turn-2'))
    assert 'opus-xhigh seat 1/1' in json.loads(run(seated))['systemMessage']


def test_fable_xhigh_draws_on_the_same_seat_as_opus_xhigh(gate):
    """Verify the two deriving tiers share one seat counter.

    Mutation: keying the seat counter on the tier, so each deriving tier
        spends the cycle's full allowance and a cap of 6 admits two.
    Oracle: at opus-cap 6, which holds one seat, opus-xhigh takes 1/1 and
        the following fable-xhigh is refused on the seat, not the round
        cap, which still has four slots free.
    """
    assert 'opus-xhigh seat 1/1' in seat_message(gate.gate_agent(xhigh('review', '6')))
    output = gate.gate_agent(fable('review', '6'))
    assert decision(output) == 'deny'
    assert 'holds 1 derive seat' in reason(output)


def test_one_brief_per_deriving_tier_fits_at_opus_cap_9(gate):
    """Verify a cycle with a brief for each deriving tier seats both at 9.

    Mutation: admitting fable-xhigh only where no opus-xhigh has seated,
        which would make the two tiers exclusive rather than sharing an
        allowance that grows with the number of deriving briefs.
    Oracle: opus-xhigh takes seat 1/2, fable-xhigh takes seat 2/2, and a
        third deriving launch is denied naming #3.
    """
    assert 'opus-xhigh seat 1/2' in seat_message(gate.gate_agent(xhigh('review', '9')))
    assert 'fable-xhigh seat 2/2' in seat_message(gate.gate_agent(fable('verify', '9')))
    output = gate.gate_agent(fable('verify', '9'))
    assert decision(output) == 'deny'
    assert '#3' in reason(output)


def test_fable_xhigh_without_a_kind_is_denied_and_takes_no_seat(gate):
    """Verify fable-xhigh requires derive: exactly as opus-xhigh does.

    Mutation: requiring the kind on opus-xhigh alone, leaving the more
        expensive tier the one that launches unmarked, or refusing after
        the seat is taken so the refusal costs the cycle its allowance.
    Oracle: the deny names the launched tier, and the seat is still free
        afterwards for an opus-xhigh launch to take as 1/1.
    """
    output = gate.gate_agent(
        agent_input(
            header('review', '6', None),
            subagent_type='agent-scope:fable-xhigh'))
    assert decision(output) == 'deny'
    assert 'agent-scope:fable-xhigh needs derive:' in reason(output)
    assert 'opus-xhigh seat 1/1' in seat_message(gate.gate_agent(xhigh('review', '6')))


def test_a_fable_model_option_is_denied_even_beside_its_tier(gate):
    """Verify fable is reachable only through the tier's frontmatter pin.

    Mutation: exempting the fable tier from the model check, or
        explaining the rule by the account default rather than the pin.
        An invocation-level model overrides the definition's version pin,
        and the fable family alias is configurable, so the launch could
        land on a version the tier never named.
    Oracle: model fable is denied on an ordinary tier and on
        agent-scope:fable-xhigh alike, both denials naming the tier; the
        same tier with no model option seats 1/1.
    """
    for launch in (
            agent_input('ordinary task', model='fable'),
            fable('review', '6', model='fable')):
        output = gate.gate_agent(launch)
        assert decision(output) == 'deny'
        assert 'agent-scope:fable-xhigh' in reason(output)
        assert 'version pin' in reason(output)
        assert 'default' not in reason(output)
    assert not state_file(gate).exists()
    output = gate.gate_agent(fable('review', '6'))
    assert 'fable-xhigh seat 1/1' in seat_message(output)


# --- Cumulative budgets and shared derive seats ---


DERIVING_TIERS = ('agent-scope:opus-xhigh', 'agent-scope:fable-xhigh')
TIER_ORDERS = [DERIVING_TIERS, DERIVING_TIERS[::-1]]


def deriving(tier, round_name='review', opus_cap='6', derive='proof', **kwargs):
    """Build a launch on one deriving tier, with a kind in its header.
    """
    return agent_input(
        header(round_name, opus_cap, derive), subagent_type=tier, **kwargs)


@pytest.mark.parametrize(
    'subagent_type',
    ['agent-scope:sonnet-medium', 'agent-scope:sonnet-high', 'agent-scope:haiku'])
def test_a_cheap_tier_launches_past_every_capped_threshold(gate, subagent_type):
    """Verify a cheap tier has no quantity limit of any kind.

    Mutation: charging a cheap tier to the round counters, or capping it
        at the largest opus-cap, which the counts here would trip.
    Oracle: thirty marked launches per round - past 9 in review, verify,
        and swarm and past 2 in synthesize - all allow, and no state file
        is written.
    """
    for round_name in gate.ROUNDS:
        payload = agent_input(header(round_name, None), subagent_type=subagent_type)
        assert all(gate.gate_agent(payload) is None for _ in range(30))
    assert not state_file(gate).exists()


def test_a_cheap_launch_declaring_a_cap_fixes_nothing(gate):
    """Verify a cheap launch cannot fix the cycle's opus-cap.

    Mutation: reserving before the uncapped exit, so a cheap agent that
        declares opus-cap 9 fixes the cycle at 9 and hands the capped
        tiers six slots and two derive seats they never declared.
    Oracle: a sonnet-high launch declaring 9 writes no state; the next
        capped launch declaring 3 allows and fixes 3, and a deriving
        launch is then refused for want of a seat.
    """
    loud = agent_input(header('review', '9'), subagent_type='agent-scope:sonnet-high')
    assert gate.gate_agent(loud) is None
    assert not state_file(gate).exists()
    assert gate.gate_agent(agent_input(header())) is None
    assert stored_cycle(gate)['opus_cap'] == '3'
    output = gate.gate_agent(deriving(DERIVING_TIERS[1], 'review', '3'))
    assert decision(output) == 'deny'
    assert 'a cycle at opus-cap 3 holds no derive seat' in reason(output)


@pytest.mark.parametrize('tier', DERIVING_TIERS)
def test_a_deriving_launch_spends_a_slot_and_a_seat(gate, tier):
    """Verify an ordinary deriving launch is charged both budgets.

    Mutation: charging the seat alone and leaving the round counter, so
        a seated derivation costs nothing against the round's cap.
    Oracle: one launch at opus-cap 6 leaves review 1 and xhigh 1 in the
        state, and its seat line names seat 1/1.
    """
    output = gate.gate_agent(deriving(tier))
    assert f'{tier.split(":")[1]} seat 1/1' in seat_message(output)
    assert stored_cycle(gate)['review'] == 1
    assert stored_cycle(gate)['xhigh'] == 1


@pytest.mark.parametrize(('first', 'second'), TIER_ORDERS)
def test_one_derive_seat_admits_one_tier_in_either_order(gate, first, second):
    """Verify the single seat at opus-cap 6 is shared, whichever tier is first.

    Mutation: a per-tier seat counter, or admitting fable-xhigh only
        where opus-xhigh has not seated; either lets a cycle at 6 spend
        two derive seats.
    Oracle: the first launch takes seat 1/1; the second is refused as
        derive seat #2 with its round counter left at 0.
    """
    assert 'seat 1/1' in seat_message(gate.gate_agent(deriving(first)))
    output = gate.gate_agent(deriving(second, 'verify', '6', 'bound'))
    assert decision(output) == 'deny'
    assert 'holds 1 derive seat; this would be #2' in reason(output)
    assert stored_cycle(gate)['xhigh'] == 1
    assert stored_cycle(gate)['verify'] == 0


@pytest.mark.parametrize(('first', 'second'), TIER_ORDERS)
def test_two_derive_seats_admit_both_tiers_in_either_order(gate, first, second):
    """Verify opus-cap 9 seats one launch of each tier and refuses a third.

    Mutation: keying the seat counter on the tier, which would refuse
        the second launch here, or letting the count run past
        XHIGH_SEATS, which would admit the third.
    Oracle: seats 1/2 and 2/2 are announced in launch order; a third
        deriving launch is refused as #3 and the seat counter stays 2.
    """
    assert 'seat 1/2' in seat_message(gate.gate_agent(deriving(first, 'review', '9')))
    assert 'seat 2/2' in seat_message(
        gate.gate_agent(deriving(second, 'verify', '9', 'bound')))
    output = gate.gate_agent(deriving(first, 'swarm', '9', 'formula'))
    assert decision(output) == 'deny'
    assert 'holds 2 derive seats; this would be #3' in reason(output)
    assert stored_cycle(gate)['xhigh'] == 2
    assert stored_cycle(gate)['swarm'] == 0


@pytest.mark.parametrize('tier', DERIVING_TIERS)
def test_a_deriving_synthesize_needs_an_unspent_seat(gate, tier):
    """Verify the synthesize round draws on the cycle's seats, not its own.

    Mutation: exempting the synthesize round from the seat counter, so a
        cycle at opus-cap 6 seats one derivation per round.
    Oracle: after a deriving review launch spends the only seat at 6, a
        deriving synthesize launch is refused as #2 although the
        synthesize round is empty.
    """
    assert 'seat 1/1' in seat_message(gate.gate_agent(deriving(tier)))
    output = gate.gate_agent(deriving(tier, 'synthesize', None, 'bound'))
    assert decision(output) == 'deny'
    assert 'holds 1 derive seat; this would be #2' in reason(output)
    assert stored_cycle(gate)['synthesize'] == 0


def test_an_admitted_slot_is_never_refunded(gate):
    """Verify a counter only rises, across a denial and a fresh process.

    Mutation: decrementing a counter when a later launch is refused, or
        rebuilding a cycle from an empty dict on a new load; either hands
        an interrupted agent's slot back and lets a relaunch overrun the
        round.
    Oracle: after two allows and a refused mismatch the stored review
        counter is 2; a second module instance reading the same state
        logs the next launch as #3 and denies the fourth.
    """
    payload = agent_input(header())
    assert all(gate.gate_agent(payload) is None for _ in range(2))
    assert decision(gate.gate_agent(agent_input(header('review', '9')))) == 'deny'
    assert stored_cycle(gate)['review'] == 2
    again = reload_gate()
    assert again.gate_agent(payload) is None
    assert last_log(again)['round_n'] == 3
    assert decision(again.gate_agent(payload)) == 'deny'
    assert stored_cycle(gate)['review'] == 3


def test_the_zero_seat_refusal_names_only_repairs_the_gate_accepts(gate):
    """Verify a cycle with no seat is never told to merge inside the cycle.

    Mutation: offering "merge this derivation into another deriving
        brief" at opus-cap 3, where the seat count is 0 and every
        deriving launch of the cycle is refused, so the caller merges
        and is refused again.
    Oracle: the refusal names opus-cap 6 or 9 and asks for no merge; the
        merged relaunch it would otherwise have suggested is refused too.
    """
    assert gate.gate_agent(agent_input(header())) is None
    output = gate.gate_agent(deriving(DERIVING_TIERS[0], 'review', '3'))
    assert decision(output) == 'deny'
    assert 'holds no derive seat' in reason(output)
    assert 'opus-cap 6 or 9' in reason(output)
    assert 'merge' not in reason(output)
    merged = deriving(DERIVING_TIERS[1], 'verify', '3', 'joint-behavior')
    assert decision(gate.gate_agent(merged)) == 'deny'


@pytest.mark.parametrize('tier', DERIVING_TIERS)
def test_the_unfixed_refusal_names_a_cap_that_seats_a_derivation(gate, tier):
    """Verify the unfixed-cycle refusal does not send the caller to cap 3.

    Mutation: naming the fixing rounds without naming the value, after
        which the documented default of 3 fixes the cycle at zero seats
        and the same synthesize launch is refused for the rest of it.
    Oracle: the refusal names opus-cap 6 or 9; a review launch at 6 then
        admits the same synthesize launch as seat 1/1.
    """
    synthesize = deriving(tier, 'synthesize', None, 'proof')
    output = gate.gate_agent(synthesize)
    assert decision(output) == 'deny'
    assert 'opus-cap 6 or 9' in reason(output)
    assert gate.gate_agent(agent_input(header(opus_cap='6'))) is None
    assert 'seat 1/1' in seat_message(gate.gate_agent(synthesize))
