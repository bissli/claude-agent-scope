"""Tests for the Workflow side of the review-gate PreToolUse hook.

Run from the repo root: `poetry run pytest tests`. The `gate` fixture in
conftest.py loads scripts/review-gate.py with its state and log redirected
to a temp dir through REVIEW_GATE_HOME.
"""

import io
import json
import pathlib
import sys

import pytest
from test_review_gate import agent_input, decision, header, last_log, reason
from test_review_gate import state_file

META = "export const meta = { name: 'probe', description: 'gate probe' }\n"
ONE_LINE_HEADER = (
    "'<review-gate>\\nround: review\\nopus-cap: 3\\n</review-gate>\\nA'")


def marked(round_name='review', opus_cap='3', tail='${args.brief}', derive=None):
    """Return a JS template-literal prompt opening with the header.
    """
    return f'`{header(round_name, opus_cap, derive)}{tail}`'


def stage(prompt_js, tier='agent-scope:opus-high', extra=''):
    """Return one agent() call as JS source.
    """
    options = f"agentType: '{tier}'" if tier is not None else ''
    if extra:
        options = f'{options}, {extra}' if options else extra
    return f'agent({prompt_js}, {{{options}}})'


def thunks(*calls):
    """Return a parallel([...]) call over one thunk per agent() call.
    """
    return 'await parallel([' + ', '.join(f'() => {call}' for call in calls) + '])'


def workflow_input(
    script=None,
    *,
    prompt_id='turn-1',
    session='session-1',
    cwd=None,
    **tool_input):
    """Build a PreToolUse payload for one Workflow call.

    Parameters
    ----------
    script : str or None
        The inline script; omitted from tool_input when None.
    prompt_id : str or None
        The cycle key; omitted from the payload when None.
    session : str
        The session id.
    cwd : pathlib.Path or None
        Added to the payload when given.
    **tool_input
        Further tool_input fields such as scriptPath or name.

    Returns
    -------
    dict
        The payload as Claude Code would send it on stdin.
    """
    payload = {
        'session_id': session,
        'tool_name': 'Workflow',
        'tool_input': dict(tool_input),
        }
    if script is not None:
        payload['tool_input']['script'] = script
    if prompt_id is not None:
        payload['prompt_id'] = prompt_id
    if cwd is not None:
        payload['cwd'] = str(cwd)
    return payload


def run(gate, script, **kwargs):
    """Gate an inline script and return the hook's decision output.
    """
    return gate.gate_workflow(workflow_input(META + script, **kwargs))


def all_logs(gate):
    """Return every gate.jsonl line as a dict.
    """
    text = (gate.STATE_HOME / 'gate.jsonl').read_text()
    return [json.loads(line) for line in text.splitlines()]


def cycle_state(gate, turn='turn-1'):
    """Return the stored cycle dict, or None when the state file has none.
    """
    path = state_file(gate)
    if not path.exists():
        return None
    return json.loads(path.read_text()).get('cycles', {}).get(turn)


EMPTY_CYCLE = {
    'opus_cap': None,
    'review': 0,
    'verify': 0,
    'swarm': 0,
    'synthesize': 0,
    }


# --- The tier rule ---


@pytest.mark.parametrize('tier', [None, 'general-purpose'])
def test_unpinned_stage_is_denied(gate, tier):
    """Verify a stage with no agentType or with general-purpose is denied.

    Mutation: treating a stage with no tier as uncapped, as an unmarked
        cheap Agent is.
    Oracle: the deny reason names every prefixed tier and the
        inherited model.
    """
    out = run(gate, 'await ' + stage("'hi'", tier))
    assert decision(out) == 'deny'
    assert (
        'agent-scope:opus-medium, agent-scope:opus-high, agent-scope:opus-xhigh, '
        'agent-scope:fable-xhigh, agent-scope:sonnet-medium, '
        'agent-scope:sonnet-high, agent-scope:haiku'
        in reason(out))
    assert 'inherit the main-loop model' in reason(out)


def test_named_type_outside_the_tiers_is_denied(gate):
    """Verify an agentType that is not one of the tiers is denied.

    Mutation: passing any named type, as the Agent rule does; Explore has
        no pin on the Workflow path and would inherit the main-loop model.
    Oracle: deny reason quotes the type and every tier.
    """
    out = run(gate, 'await ' + stage("'hi'", 'Explore'))
    assert decision(out) == 'deny'
    assert 'agentType Explore is not a tier' in reason(out)


def test_fork_is_not_a_workflow_tier(gate):
    """Verify a fork stage is denied as a type outside the tiers.

    Mutation: adding fork to TIERS for parity with the Agent
        path, where a fork counts as Opus; here fork is not in OPUS_TIERS,
        so the stage would read as cheap, unmarked, and uncounted.
    Oracle: deny reason quotes fork and every tier; no state written.
    """
    out = run(gate, 'await ' + stage("'hi'", 'fork'))
    assert decision(out) == 'deny'
    assert 'agentType fork is not a tier' in reason(out)
    assert cycle_state(gate) is None


def test_bare_tier_name_in_workflow_is_denied(gate):
    """Verify a Workflow agentType without the plugin prefix is denied.

    Mutation: dropping the Workflow bare-name branch, so a bare tier is
        denied with the generic not-a-tier message and never told the
        prefix.
    Oracle: agentType 'opus-high' is denied naming the plugin prefix;
        no state is written.
    """
    out = run(gate, 'await ' + stage("'hi'", 'opus-high'))
    assert decision(out) == 'deny'
    assert "plugin's agents" in reason(out)
    assert 'agent-scope:opus-high' in reason(out)
    assert 'not opus-high' in reason(out)
    assert cycle_state(gate) is None


def test_workflow_agent_type_compares_exactly(gate):
    """Verify a Workflow agentType with the prefix in another case is denied.

    Mutation: lowercasing agentType before the tier lookup, for parity
        with the Agent path, so 'Agent-Scope:opus-high' passes as a tier
        and takes a slot for a type the runtime rejects.
    Oracle: the exact-case literal is denied as not a tier; no state is
        written.
    """
    out = run(gate, 'await ' + stage("'hi'", 'Agent-Scope:opus-high'))
    assert decision(out) == 'deny'
    assert 'Agent-Scope:opus-high is not a tier' in reason(out)
    assert cycle_state(gate) is None


def test_prefixed_general_purpose_stage_is_not_a_tier(gate):
    """Verify agentType 'agent-scope:general-purpose' is denied as not a tier.

    Mutation: testing INHERITING_TYPES on the stripped stage tier, so the
        denial claims a stage inherits the main-loop model.
    Oracle: the denial names the stage as not a tier and never says
        inherit.
    """
    out = run(gate, 'await ' + stage("'hi'", 'agent-scope:general-purpose'))
    assert decision(out) == 'deny'
    assert 'agent-scope:general-purpose is not a tier' in reason(out)
    assert 'inherit' not in reason(out)


def test_prefixed_typo_tier_in_workflow_is_denied(gate):
    """Verify a prefixed but misspelled tier in a Workflow is denied.

    Mutation: dropping the prefixed-non-tier check in the Workflow path
        so agent-scope:sonnet-hgih passes as a valid tier.
    Oracle: agentType 'agent-scope:sonnet-hgih' is denied naming the
        seven valid tiers; no state is written.
    """
    out = run(gate, 'await ' + stage("'hi'", 'agent-scope:sonnet-hgih'))
    assert decision(out) == 'deny'
    assert 'agent-scope:sonnet-hgih is not a tier' in reason(out)
    assert 'agent-scope:opus-medium' in reason(out)
    assert cycle_state(gate) is None


@pytest.mark.parametrize('option', ["model: 'haiku'", "effort: 'low'"])
def test_model_and_effort_options_are_denied(gate, option):
    """Verify a model or effort key in the options is denied on any tier.

    Mutation: denying only model: 'fable', as the Agent rule does; on the
        Workflow path a per-call model or effort outranks the tier's pin.
    Oracle: deny reason names the key and says the tier pins both.
    """
    out = run(gate, 'await ' + stage("'hi'", 'agent-scope:haiku', option))
    assert decision(out) == 'deny'
    assert option.split(':')[0] in reason(out)
    assert 'the tier pins the model and effort' in reason(out)


@pytest.mark.parametrize('declarator', ['const', 'let', 'var'])
def test_meta_phase_model_is_denied(gate, declarator):
    """Verify a model key on a meta phase entry is denied.

    Mutation: reading only agent() options for the pinned keys, or only a
        meta declared with const.
    Oracle: deny reason names the meta phase and the key.
    """
    script = (
        f"export {declarator} meta = {{ name: 'p', description: 'd', "
        "phases: [{ title: 'R', model: 'opus' }] }\n"
        'await ' + stage("'hi'", 'haiku'))
    out = gate.gate_workflow(workflow_input(script))
    assert decision(out) == 'deny'
    assert 'meta phase' in reason(out)
    assert 'model' in reason(out)


@pytest.mark.parametrize(('options', 'fragment'), [
    ('o', 'must be one object literal'),
    ("{...base, agentType: 'haiku'}", 'not key: value'),
    ('{agentType}', 'not key: value'),
    ("{['agentType']: 'haiku'}", 'not key: value'),
    ("{agentType: 'haiku', agentType: 'opus-high'}", 'repeats agentType'),
    ('{agentType: TIER}', 'must be a string literal'),
    ("{agentType: ['haiku'][0]}", 'must be a string literal'),
    ])
def test_options_the_gate_cannot_read_are_denied(gate, options, fragment):
    """Verify options that hide the tier from a static read are denied.

    Mutation: accepting an identifier, a spread, a computed key, or the
        last of two agentType keys as the tier.
    Oracle: deny reason carries the fragment naming the unreadable form.
    """
    script = "const o = {agentType: 'haiku'}, base = {}, TIER = 'haiku'\n"
    script += f"await agent('hi', {options})"
    out = run(gate, script)
    assert decision(out) == 'deny'
    assert fragment in reason(out)


def test_escaped_tier_literal_is_read_cooked(gate):
    r"""Verify the tier is read as the runtime sees it, escapes decoded.

    Mutation: comparing the raw source text with the tier names.
    Oracle: 'agent-scope:opus\x2dhigh' is allowed and logged as
        agent-scope:opus-high.
    """
    script = (
        'await agent(' + ONE_LINE_HEADER
        + ", {agentType: 'agent-scope:opus\\x2dhigh'})")
    assert run(gate, script) is None
    assert last_log(gate)['agent_type'] == 'agent-scope:opus-high'


@pytest.mark.parametrize('call', [
    'agent()',
    "agent('a', {agentType: 'haiku'}, 3)",
    "agent( , {agentType: 'haiku'})",
    ])
def test_agent_call_takes_a_prompt_and_options_only(gate, call):
    """Verify agent() with no, an empty, or a third argument is denied.

    Mutation: reading the first two arguments and ignoring the rest, or
        indexing an empty argument (a crash, which fails open).
    Oracle: deny reason says agent() takes a prompt and an options object.
    """
    out = run(gate, 'await ' + call)
    assert decision(out) == 'deny'
    assert 'takes a prompt and an options object' in reason(out)


# --- The header on a stage ---


def test_opus_stage_with_a_dynamic_prompt_is_denied(gate):
    """Verify an Opus stage whose prompt is not a leading literal is denied.

    Mutation: treating an unreadable prompt as unmarked and letting the
        Opus stage through uncounted.
    Oracle: deny reason asks for a leading string or template literal and
        names the shapes that hide the header.
    """
    out = run(gate, 'await ' + stage('args.prompt', 'agent-scope:opus-high'))
    assert decision(out) == 'deny'
    assert 'opens its prompt with a string or template literal' in reason(out)
    concat = 'const HEADER = ' + ONE_LINE_HEADER + '\n'
    concat += 'await ' + stage("HEADER + 'brief'", 'agent-scope:opus-high')
    assert 'a concatenation, or a helper call hides it' in reason(run(gate, concat))


def test_leading_substitution_hides_the_header(gate):
    """Verify a template opening with ${...} is denied for hiding the header.

    Mutation: treating an empty static prefix as an unmarked prompt, so
        the deny sends the author to round: swarm although the header sits
        in the substituted variable.
    Oracle: the deny names the leading substitution, not round: swarm; the
        same header as the literal's first text, then a substitution,
        allows.
    """
    script = 'const HEADER = ' + ONE_LINE_HEADER + '\n'
    out = run(
        gate, script + 'await ' + stage('`${HEADER}brief`', 'agent-scope:opus-high'))
    assert decision(out) == 'deny'
    assert 'a leading ${...} hides it' in reason(out)
    assert 'round: swarm' not in reason(out)
    literal_first = stage(marked(tail='${brief}'), 'agent-scope:opus-high')
    assert run(gate, 'const brief = 1\nawait ' + literal_first) is None


def test_cheap_stage_with_a_dynamic_prompt_is_allowed(gate):
    """Verify a sonnet or haiku stage may build its prompt at runtime.

    Mutation: applying the literal-prompt rule to every tier.
    Oracle: allowed, logged not-review-marked, no state file.
    """
    assert run(gate, 'await ' + stage('args.prompt', 'agent-scope:sonnet-high')) is None
    assert last_log(gate)['scope'] == 'not-review-marked'
    assert cycle_state(gate) is None


def test_template_prefix_before_a_substitution_is_the_header(gate):
    """Verify the header is read from a template's static text before `${`.

    Mutation: reading only templates with no substitution.
    Oracle: the stage takes review slot 1 of 3 under opus-cap 3.
    """
    assert run(gate, 'await ' + stage(marked())) is None
    line = last_log(gate)
    assert (line['round'], line['round_n'], line['round_cap']) == ('review', 1, 3)


def test_string_escapes_in_the_header_are_decoded(gate):
    r"""Verify a single-quoted prompt with \n escapes carries a header.

    Mutation: matching the header regex against the raw source text, where
        the escaped newlines are two characters.
    Oracle: the stage is counted with round verify.
    """
    prompt = (
        "'<review-gate>\\nround: verify\\nopus-cap: 3\\n</review-gate>\\n"
        "Check.'")
    assert run(gate, 'await ' + stage(prompt, 'agent-scope:opus-medium')) is None
    assert last_log(gate)['round'] == 'verify'
    assert cycle_state(gate)['verify'] == 1


@pytest.mark.parametrize(('prompt', 'fragment'), [
    ('`<review-gate>\\nround: review\\n`', 'not closed correctly'),
    ('`<review-gate>\\nround: audit\\n</review-gate>\\n`', 'round must be'),
    (
        '`<review-gate>\\nround: review\\nopus-cap: 12\\n</review-gate>\\n`',
        'invalid opus-cap'),
    ])
def test_header_grammar_is_checked_on_a_cheap_stage(gate, prompt, fragment):
    """Verify a malformed or invalid header is denied on a haiku stage.

    Mutation: skipping header validation for uncapped tiers.
    Oracle: deny reason carries the Agent-path wording for the fault.
    """
    out = run(gate, 'await ' + stage(prompt, 'agent-scope:haiku'))
    assert decision(out) == 'deny'
    assert fragment in reason(out)


def test_unmarked_opus_stage_is_denied(gate):
    """Verify an Opus stage with a literal prompt and no header is denied.

    Mutation: letting an unmarked Opus stage through uncounted, or
        applying the rule to a cheap stage.
    Oracle: the deny names the stage line and round: swarm and writes no
        state; the same prompt on sonnet-high is allowed.
    """
    out = run(gate, 'await ' + stage("'Judge this.'"))
    assert decision(out) == 'deny'
    assert 'Workflow stage at line 2' in reason(out)
    assert 'round: swarm' in reason(out)
    assert cycle_state(gate) is None
    assert run(
        gate, 'await ' + stage("'Judge this.'", 'agent-scope:sonnet-high')) is None


def test_swarm_stages_have_their_own_counter(gate):
    """Verify swarm stages are capped like review stages, on their own counter.

    Mutation: letting a swarm stage through uncounted, or charging it to
        the review counter so three reviews and a swarm overrun opus-cap 3.
    Oracle: three review stages and a swarm stage are allowed with review
        3 and swarm 1; a script of four swarm stages is refused whole at
        most 3 in the swarm round, moving no counter.
    """
    script = thunks(
        stage(marked()), stage(marked()), stage(marked()),
        stage(marked('swarm')))
    assert run(gate, script) is None
    assert (cycle_state(gate)['review'], cycle_state(gate)['swarm']) == (3, 1)
    four = thunks(*[stage(marked('swarm')) for _ in range(4)])
    out = run(gate, four, prompt_id='turn-2')
    assert decision(out) == 'deny'
    assert 'at most 3 capped agents in the swarm round' in reason(out)
    assert cycle_state(gate, 'turn-2') == EMPTY_CYCLE


def test_swarm_stage_on_xhigh_needs_a_kind_and_takes_the_seat(gate):
    """Verify an opus-xhigh swarm stage declares a kind and takes a seat.

    Mutation: exempting the swarm round from the derive rule on a stage,
        or returning silence on the seated script.
    Oracle: denied naming derive: <kind> without it; allowed with it,
        returning a systemMessage that names the stage, with the cycle's
        xhigh counter at 1.
    """
    out = run(gate, 'await ' + stage(marked('swarm'), 'agent-scope:opus-xhigh'))
    assert decision(out) == 'deny'
    assert 'needs derive: <kind>' in reason(out)
    swarm = 'await ' + stage(
        marked('swarm', '6', derive='bound'), 'agent-scope:opus-xhigh')
    out = run(gate, swarm)
    assert decision(out) is None
    assert 'opus-xhigh seat 1/1 - derive: bound - Workflow stage at line' in (
        out['systemMessage'])
    assert cycle_state(gate)['xhigh'] == 1


def test_opus_review_stage_without_opus_cap_is_denied(gate):
    """Verify an Opus review stage needs an opus-cap, as an Agent does.

    Mutation: defaulting a missing opus-cap to 3.
    Oracle: deny reason asks for opus-cap in the leading header.
    """
    out = run(gate, 'await ' + stage(marked('review', None)))
    assert decision(out) == 'deny'
    assert 'require opus-cap' in reason(out)


def test_xhigh_stage_without_a_kind_is_denied_before_a_slot(gate):
    """Verify an opus-xhigh stage with no derive is denied before a slot.

    Mutation: checking the derive rule after the reservation.
    Oracle: deny names opus-high as the remedy and no state is written.
    """
    out = run(gate, 'await ' + stage(marked(), 'agent-scope:opus-xhigh'))
    assert decision(out) == 'deny'
    assert 'needs derive: <kind>' in reason(out)
    assert 'use agent-scope:opus-high' in reason(out)
    assert cycle_state(gate) is None


@pytest.mark.parametrize('tier', ['agent-scope:opus-high', 'agent-scope:sonnet-high'])
def test_derive_on_a_stage_below_xhigh_is_denied(gate, tier):
    """Verify a stage on any other tier may not carry derive.

    Mutation: checking the tier rule only on Opus stages, so a cheap
        stage carries the field unread.
    Oracle: an opus-high and a sonnet-high stage declaring derive: proof
        are denied naming the stage's tier; no state is written.
    """
    out = run(gate, 'await ' + stage(marked(derive='proof'), tier))
    assert decision(out) == 'deny'
    expected = f'derive belongs on a deriving tier; {tier} declared derive: proof'
    assert expected in reason(out)
    assert cycle_state(gate) is None


# --- Single-shot Opus stages ---


@pytest.mark.parametrize(('script', 'fragment'), [
    ('await pipeline(args, d => ' + stage(marked()) + ')', 'a function'),
    ('await parallel(args.map(a => () => ' + stage(marked()) + '))', 'a function'),
    ('for (const x of args) { await ' + stage(marked()) + ' }', 'a for loop'),
    ('for (const x of args) log(x), await ' + stage(marked()), 'a for loop'),
    ('while (args.more) { await ' + stage(marked()) + ' }', 'a while loop'),
    ('do { await ' + stage(marked()) + ' } while (args.more)', 'a do loop'),
    (
        'async function go() { return ' + stage(marked()) + ' }\nawait go()',
        'a function'),
    (
        'const go = () => ' + stage(marked()) + '\nawait parallel([go, go])',
        'a function'),
    (
        'const o = { run() { return ' + stage(marked()) + ' } }\nawait o.run()',
        'a function body'),
    ('await (async () => { await ' + stage(marked()) + ' })()', 'a function'),
    (
        'await parallel(Array.from({length: 3}, () => () => ' + stage(marked()) + '))',
        'a function'),
    (
        'await parallel([() => ' + stage(marked()) + '].flatMap(t => [t, t]))',
        'a function'),
    ('await args.x.parallel([() => ' + stage(marked()) + '])', 'a function'),
    (
        'await parallel(args.map(a => ' + stage(marked()) + '.then(v => v)))',
        'a function'),
    (
        'const s = JSON.stringify(await ' + stage(marked()) + ')',
        'a call to .stringify()'),
    ('class R { async run() { return ' + stage(marked()) + ' } }', 'a function body'),
    ('class R { static go = ' + stage(marked()) + ' }', 'a class'),
    ('for (const x of xs) if (x) log(x); else await ' + stage(marked()), 'a for loop'),
    ('while (more) if (a) log(1); else await ' + stage(marked()), 'a while loop'),
    ('for (const x of xs) o = {a: x}, await ' + stage(marked()), 'a for loop'),
    ('for (const x of xs) s = `a${x}b`, await ' + stage(marked()), 'a for loop'),
    (
        'for (const x of xs) if (x) { log(x) } else await ' + stage(marked()),
        'a for loop'),
    (
        'for (const x of xs) if (x) log(x)\n  else await ' + stage(marked()),
        'a for loop'),
    ('for (const x of xs)\n  await ' + stage(marked()), 'a for loop'),
    ('const run = (n) => `pre${n}post` && ' + stage(marked()), 'a function'),
    ('const g = (x = ' + stage(marked()) + ') => x\nawait g()', 'a function'),
    ('const g = ({x = ' + stage(marked()) + '}) => x\nawait g()', 'a function'),
    ('function f()\n{ return ' + stage(marked()) + ' }', 'a function body'),
    ('for (const q of xs) if (0) ; else await ' + stage(marked()), 'a for loop'),
    (
        'for (const q of xs) try {} finally { await ' + stage(marked()) + ' }',
        'a for loop'),
    (
        'for (const q of xs) try { log(q) } catch (e) { await '
        + stage(marked()) + ' }',
        'a for loop'),
    (
        'do try {} finally { await ' + stage(marked()) + ' } while (++n < 3)',
        'a do loop'),
    ])
def test_opus_stage_under_a_multiplier_is_denied(gate, script, fragment):
    """Verify an Opus stage that may run more than once is denied.

    Mutation: bounding a statement at a comma (the comma-operator loop
        body), at the `;` or `}` of an if arm, at an object literal's or a
        template substitution's `}`, at a `try` or `catch` arm's `}`, or at a
        line break before `else`;
        reading only the innermost bracket (the .then inside .map);
        skipping the token after a thunk array (the .flatMap); taking any
        callee named parallel (the dotted one); missing the `=>` after a
        parameter list.
    Oracle: deny reason names the construct and no state is written.
    """
    out = run(gate, script)
    assert decision(out) == 'deny'
    assert 'may run more than once' in reason(out)
    assert fragment in reason(out)
    assert cycle_state(gate) is None


@pytest.mark.parametrize('script', [
    'const r = await ' + stage(marked()),
    thunks(stage(marked()), stage(marked(tail='B'))),
    'await parallel([async () => { const a = await ' + stage(marked())
    + '; return a }])',
    'await ' + stage(marked()) + '.then(v => '
    + stage(marked('verify'), 'agent-scope:opus-medium') + ')',
    'if (args.deep) { await ' + stage(marked()) + ' } else { log("skip") }',
    'try { await ' + stage(marked()) + ' } catch (e) { log(String(e)) }',
    'switch (args.k) { case 1: await ' + stage(marked()) + '; break }',
    'for (const x of args) { log(x) }\nawait ' + stage(marked()),
    'await Promise.all([' + stage(marked()) + ', ' + stage(marked(tail='B')) + '])',
    'const out = { first: await ' + stage(marked()) + ' }',
    'const r = args.deep ? await ' + stage(marked()) + ' : null',
    'await ' + stage(marked(tail='${args.items.map(a => `- ${a}`).join("\\n")}')),
    'await other().catch(v => ' + stage(marked()) + ')',
    'const pick = (a, b) => a\nawait ' + stage(marked()),
    'do { log(1) } while (args.more)\nawait ' + stage(marked()),
    'for (const x of xs) log(x)\nawait ' + stage(marked()),
    'let i = 1; const r = i++ / 2\nawait ' + stage(marked()),
    'await parallel([() => ' + stage(marked()) + ',],)',
    'const m = { agent: 1 }\nawait ' + stage(marked()),
    'console.log(1)\nawait ' + stage(marked()),
    ])
def test_single_shot_opus_stage_is_allowed(gate, script):
    """Verify the accepted single-shot shapes pass and are counted.

    Mutation: rejecting an arrow anywhere (the parallel thunks and the
        continuations), or a brace anywhere (if, try, switch, object);
        reading no statement end at a line break after a value (the arrow
        before `await`, the loop body before it); taking the `while` of a
        do block as a loop head; reading `i++ /` as a regex; requiring `)`
        right after the thunk array; reading an `agent:` key as an alias.
    Oracle: allowed and the review counter equals the review stages logged.
    """
    assert run(gate, script) is None
    counted = [line for line in all_logs(gate) if line.get('round_n')]
    assert counted
    reviews = [line for line in counted if line['round'] == 'review']
    assert cycle_state(gate)['review'] == len(reviews)


def test_cheap_stages_fan_out_freely(gate):
    """Verify a sonnet stage inside pipeline() and a loop is allowed.

    Mutation: applying the single-shot rule to every tier.
    Oracle: allowed, both stages logged uncapped or unmarked, no state.
    """
    sweep = stage('d.prompt', 'agent-scope:sonnet-high')
    script = (
        'const rs = await pipeline(args, d => ' + sweep + ')\n'
        'for (const r of rs) { await ' + stage(marked(), 'agent-scope:haiku') + ' }')
    assert run(gate, script) is None
    scopes = [line['scope'] for line in all_logs(gate)]
    assert scopes == ['not-review-marked', 'uncapped']
    assert cycle_state(gate) is None


# --- Reservation across a script and across tools ---


def test_stages_share_the_cycle_counters_with_agent_launches(gate):
    """Verify a script's Opus stages and Agent launches count together.

    Mutation: keeping a separate counter per tool.
    Oracle: two Agent launches then a two-stage script is denied as #4,
        and a one-stage script then fits as #3.
    """
    assert gate.gate_agent(agent_input(header())) is None
    assert gate.gate_agent(agent_input(header())) is None
    out = run(gate, thunks(stage(marked()), stage(marked(tail='B'))))
    assert decision(out) == 'deny'
    assert 'this would be #4' in reason(out)
    assert cycle_state(gate)['review'] == 2
    assert run(gate, 'await ' + stage(marked())) is None
    assert cycle_state(gate)['review'] == 3
    assert decision(gate.gate_agent(agent_input(header()))) == 'deny'


def test_refused_script_moves_no_counter(gate):
    """Verify a script refused on its fourth stage takes none of the slots.

    Mutation: committing each stage's slot as it is decided.
    Oracle: after the denial the review counter is 0 and the opus-cap is
        not fixed, so a script declaring 6 still fits.
    """
    out = run(gate, thunks(*(stage(marked(tail=str(i))) for i in range(4))))
    assert decision(out) == 'deny'
    assert 'this would be #4' in reason(out)
    assert cycle_state(gate) == EMPTY_CYCLE
    six = thunks(*(stage(marked(opus_cap='6', tail=str(i))) for i in range(6)))
    assert run(gate, six) is None
    assert cycle_state(gate)['review'] == 6


def test_mismatch_inside_one_script_is_denied_and_fixes_nothing(gate):
    """Verify stage two declaring 6 after stage one declared 3 is denied.

    Mutation: persisting the opus-cap stage one fixed before stage two
        was refused.
    Oracle: deny names 3 as the declared cycle; state has no fix.
    """
    second = stage(marked(opus_cap='6', tail='B'))
    out = run(gate, thunks(stage(marked()), second))
    assert decision(out) == 'deny'
    assert 'was declared opus-cap 3' in reason(out)
    assert last_log(gate)['fixed'] == '3'
    assert cycle_state(gate)['opus_cap'] is None


def test_rounds_keep_separate_counters_in_one_script(gate):
    """Verify three review and three verify stages fit under opus-cap 3.

    Mutation: charging every stage to one counter.
    Oracle: state shows review 3 and verify 3.
    """
    calls = [stage(marked(tail=str(i))) for i in range(3)]
    calls += [
        stage(marked('verify', tail=str(i)), 'agent-scope:opus-medium')
        for i in range(3)]
    assert run(gate, thunks(*calls)) is None
    assert (cycle_state(gate)['review'], cycle_state(gate)['verify']) == (3, 3)


def test_second_xhigh_seat_in_a_script_is_denied(gate):
    """Verify two opus-xhigh stages at opus-cap 6 are refused on the seat.

    Mutation: counting an opus-xhigh stage against the Opus cap alone and
        never against the cycle's seats.
    Oracle: deny names #2 of 1 seat and opus-high as the remedy, and the
        script moves no counter.
    """
    calls = [
        stage(marked(opus_cap='6', derive='proof'), 'agent-scope:opus-xhigh'),
        stage(marked(opus_cap='6', tail='B', derive='proof'), 'agent-scope:opus-xhigh'),
        ]
    out = run(gate, thunks(*calls))
    assert decision(out) == 'deny'
    assert 'holds 1 derive seat; this would be #2' in reason(out)
    assert 'opus-high' in reason(out)
    assert cycle_state(gate) == EMPTY_CYCLE


def test_xhigh_stage_at_opus_cap_3_is_refused_on_the_seat(gate):
    """Verify a script cannot seat an opus-xhigh at the default cap.

    Mutation: a nonzero seat count at 3, or the zero-seat wording lost on
        the Workflow path.
    Oracle: one opus-xhigh stage at 3 with a kind is denied naming no
        seat and opus-cap 6 or 9, and the script fixes nothing.
    """
    out = run(gate, 'await ' + stage(marked(derive='proof'), 'agent-scope:opus-xhigh'))
    assert decision(out) == 'deny'
    assert 'a cycle at opus-cap 3 holds no derive seat' in reason(out)
    assert 'opus-cap 6 or 9' in reason(out)
    assert cycle_state(gate) == EMPTY_CYCLE


def test_two_xhigh_stages_fit_at_opus_cap_9(gate):
    """Verify a cycle at 9 seats two opus-xhigh stages in one script.

    Mutation: a seat count of one at every cap, or one message for a
        script with two seated stages.
    Oracle: two opus-xhigh stages at 9 allow, the systemMessage carries
        seat 1/2 and seat 2/2 on two lines, and the cycle holds xhigh 2.
    """
    calls = [
        stage(marked(opus_cap='9', derive='proof'), 'agent-scope:opus-xhigh'),
        stage(
            marked(opus_cap='9', tail='B', derive='interleaving'),
            'agent-scope:opus-xhigh'),
        ]
    out = run(gate, thunks(*calls))
    assert decision(out) is None
    lines = out['systemMessage'].split('\n')
    assert [line.split(' - ')[0] for line in lines] == [
        'review-gate: opus-xhigh seat 1/2',
        'review-gate: opus-xhigh seat 2/2',
        ]
    assert cycle_state(gate)['xhigh'] == 2


def test_synthesize_cap_holds_in_a_script(gate):
    """Verify a third synthesize stage is denied.

    Mutation: taking the round's cap from CAPS.
    Oracle: deny names the synthesize cap of 2 and #3.
    """
    calls = [stage(marked('synthesize', None, str(i))) for i in range(3)]
    out = run(gate, thunks(*calls))
    assert decision(out) == 'deny'
    assert 'at most 2 capped agents per cycle; this would be #3' in reason(out)


def test_unresolved_cycle_key_allows_and_logs(gate):
    """Verify a script with no prompt_id and no transcript is allowed.

    Mutation: denying when the cycle cannot be keyed.
    Oracle: allowed and the counted stage logs scope no-turn.
    """
    assert run(gate, 'await ' + stage(marked()), prompt_id=None) is None
    assert last_log(gate)['scope'] == 'no-turn'


# --- Where the script comes from ---


def test_script_path_is_read_and_gated(gate, tmp_path):
    """Verify a scriptPath file is read and its stages gated.

    Mutation: gating only the inline script field.
    Oracle: the file's unpinned stage is denied and the log names the path.
    """
    path = tmp_path / 'flow.js'
    path.write_text(META + "await agent('hi')")
    out = gate.gate_workflow(workflow_input(scriptPath=str(path)))
    assert decision(out) == 'deny'
    assert 'name the tier' in reason(out)
    assert last_log(gate)['source'] == str(path)


def test_relative_script_path_resolves_against_cwd(gate, tmp_path):
    """Verify a relative scriptPath is joined to the payload's cwd.

    Mutation: resolving against the hook's own working directory.
    Oracle: the file under cwd is found and its stage counted.
    """
    (tmp_path / 'flow.js').write_text(META + 'await ' + stage(marked()))
    payload = workflow_input(scriptPath='flow.js', cwd=tmp_path)
    assert gate.gate_workflow(payload) is None
    assert cycle_state(gate)['review'] == 1


def test_saved_name_is_denied_whatever_file_carries_it(gate, tmp_path, monkeypatch):
    """Verify a saved workflow name is denied even when a matching file exists.

    Mutation: resolving the name to .claude/workflows/<name>.js and gating
        that file; the runtime resolves by meta.name, so that file need not
        be the script it runs.
    Oracle: deny names the saved name and reads no file.
    """
    saved = tmp_path / '.claude' / 'workflows' / 'saved.js'
    saved.parent.mkdir(parents=True)
    saved.write_text(META + 'await ' + stage("'hi'", 'haiku'))
    monkeypatch.setattr(pathlib.Path, 'home', classmethod(lambda cls: tmp_path))
    out = gate.gate_workflow(workflow_input(name='saved', cwd=tmp_path))
    assert decision(out) == 'deny'
    assert 'saved workflow saved resolves to' in reason(out)
    assert last_log(gate)['source'] == 'saved'


@pytest.mark.parametrize('tool_input', [
    {'name': 'review-changes'},
    {'scriptPath': 'missing.js'},
    {},
    ])
def test_unreadable_source_is_denied(gate, tmp_path, monkeypatch, tool_input):
    """Verify a call whose script the gate cannot read is denied.

    Mutation: allowing a saved or built-in workflow the hook cannot see.
    Oracle: deny reason says to pass the script inline or as scriptPath.
    """
    monkeypatch.setattr(pathlib.Path, 'home', classmethod(lambda cls: tmp_path))
    out = gate.gate_workflow(workflow_input(cwd=tmp_path, **tool_input))
    assert decision(out) == 'deny'
    assert 'pass the script inline or as a readable scriptPath' in reason(out)


# --- Reading the script ---


@pytest.mark.parametrize(('script', 'fragment'), [
    (
        "const a = agent\nawait a('x', {agentType: 'haiku'})",
        'used other than as a call'),
    ("await [agent][0]('x', {agentType: 'haiku'})", 'used other than as a call'),
    ("await agent?.('x', {agentType: 'haiku'})", 'used other than as a call'),
    ("eval('agent(1)')", 'eval at line'),
    ("await Function('return agent')()('x')", 'Function at line'),
    ("await globalThis['agent']('x', {agentType: 'haiku'})", 'globalThis at line'),
    ("await [].constructor.constructor('return agent')()('x')", 'constructor at line'),
    ("await workflow('review-changes', args)", 'workflow at line'),
    ("const m = await import('x')", 'import at line'),
    ("const fs = require('fs')", 'require at line'),
    ("await \\u0061gent('x', {agentType: 'haiku'})", 'a backslash outside a literal'),
    (
        ('const parallel = (ts) => ts.flatMap(t => [t(), t()])\n'
         "await parallel([() => agent('x', {agentType: 'haiku'})])"),
        'parallel at line 2 may not be declared'),
    (
        ('let parallel\nparallel = (ts) => ts.flatMap(t => [t(), t()])\n'
         "await parallel([() => agent('x', {agentType: 'haiku'})])"),
        'parallel at line 2 may not be declared'),
    (
        "const run = (parallel) => parallel([() => agent('x', {agentType: 'haiku'})])",
        'parallel at line 2 may only be called'),
    (
        ('const fake = { then: (f) => [f(), f()] }\n'
         "await fake.then(v => agent('x', {agentType: 'haiku'}))"),
        'then at line 2 may only be a property call'),
    (
        ('class Fake { then(f) { return [f(), f()] } }\n'
         "await new Fake().then(v => agent('x', {agentType: 'haiku'}))"),
        'then at line 2 may only be a property call'),
    (
        ('const fake = { catch(f) { return [f(), f()] } }\n'
         "await fake.catch(v => agent('x', {agentType: 'haiku'}))"),
        'catch at line 2 may not be an object member'),
    (
        ('const fake = { a: 1, finally: (f) => [f(), f()] }\n'
         "await fake.finally(v => agent('x', {agentType: 'haiku'}))"),
        'finally at line 2 may not be an object member'),
    (
        ('function parallel(fs) { return fs.flatMap(f => [f(), f()]) }\n'
         "await parallel([() => agent('x', {agentType: 'haiku'})])"),
        'parallel at line 2 may not be declared'),
    (
        "function agent(p, o) { return 1 }\nawait agent('x', {agentType: 'haiku'})",
        'agent at line 2 may not be declared'),
    (
        ("let i = 0, x = 1\ni++ /x; await agent('p', {agentType: 'general-purpose'});"
         ' 1/ 1'),
        'name the tier'),
    ])
def test_paths_to_agent_the_gate_cannot_see_are_denied(gate, script, fragment):
    """Verify aliases, reflection, escapes, and rebound helpers are denied.

    Mutation: matching only the literal text agent( and passing the rest;
        trusting parallel, then, catch, and finally by name alone while a
        declaration rebinds them; reading `/` after `++` as a regex start,
        which swallows the rest of the line.
    Oracle: deny reason names the construct and its line.
    """
    out = run(gate, script)
    assert decision(out) == 'deny'
    assert fragment in reason(out)


@pytest.mark.parametrize(('script', 'fragment'), [
    ("await agent('x', {agentType: 'haiku'}", 'an unclosed ('),
    ("await agent('x', {agentType: 'haiku'}))", 'an unmatched )'),
    ("await agent('x\n', {agentType: 'haiku'})", 'an unterminated string'),
    ('await agent(`x', 'an unterminated template'),
    (
        "const re = /abc\nawait agent('x', {agentType: 'haiku'})",
        'an unterminated regex'),
    ("/* open\nawait agent('x', {agentType: 'haiku'})", 'an unterminated comment'),
    ])
def test_script_the_gate_cannot_parse_is_denied(gate, script, fragment):
    """Verify an unbalanced or unterminated script is denied, not misread.

    Mutation: allowing a script whose scan ends inside a literal.
    Oracle: deny reason names the fault and its line.
    """
    out = run(gate, script)
    assert decision(out) == 'deny'
    assert fragment in reason(out)


def test_agent_in_comments_and_strings_is_not_a_stage(gate):
    """Verify agent( inside a comment or a string is plain text.

    Mutation: scanning the raw text for agent( with a regex.
    Oracle: the one real stage is logged and nothing else.
    """
    script = (
        "// agent('in a comment')\n/* agent('in a block') */\n"
        "await agent('the agent( token in a prompt', {agentType: 'agent-scope:haiku'})")
    assert run(gate, script) is None
    assert len(all_logs(gate)) == 1


def test_regex_and_division_are_told_apart(gate):
    """Verify a regex holding a quote and a division both scan cleanly.

    Mutation: reading every / as division (the quote in the regex opens a
        string that never closes) or as a regex (the division runs to the
        end of the line).
    Oracle: both scripts are allowed.
    """
    regex = (
        "const re = /ag'ent\\//g\n"
        "await agent('x', {agentType: 'agent-scope:haiku'})")
    division = (
        'const n = args.a / args.b\n'
        "await agent('x', {agentType: 'agent-scope:haiku'})")
    assert run(gate, regex) is None
    assert run(gate, division) is None


def test_property_named_agent_is_not_a_stage(gate):
    """Verify obj.agent(...) is not the agent function.

    Mutation: treating every agent( as a stage.
    Oracle: the call passes with scope no-stages.
    """
    assert run(gate, "await args.client.agent('x')") is None
    assert last_log(gate)['scope'] == 'no-stages'


def test_no_stages_logs_one_line(gate):
    """Verify a script with no agent() call is allowed and logged.

    Mutation: denying a script that launches nothing.
    Oracle: one log line with scope no-stages.
    """
    assert run(gate, 'return args') is None
    assert [line['scope'] for line in all_logs(gate)] == ['no-stages']


# --- The log and the CLI ---


def test_log_carries_one_line_per_stage(gate):
    """Verify each stage logs its line, label, tier, and slot.

    Mutation: logging one line for the whole script.
    Oracle: two lines, in source order, with the fields of an Agent line
        plus line and source.
    """
    script = (
        'await parallel([\n'
        '  () => ' + stage(ONE_LINE_HEADER, extra="label: 'review:bugs'") + ',\n'
        '  () => '
        + stage('args.p', 'agent-scope:sonnet-high', extra="label: 'sweep'")
        + ',\n'
        '])')
    assert run(gate, script) is None
    first, second = all_logs(gate)
    assert (first['tool'], first['line'], first['label'], first['agent_type']) == (
        'Workflow', 3, 'review:bugs', 'agent-scope:opus-high')
    assert (first['round'], first['round_n'], first['round_cap'], first['source']) == (
        'review', 1, 3, 'script')
    assert (second['line'], second['label'], second['scope']) == (
        4, 'sweep', 'not-review-marked')


def test_main_routes_a_workflow_payload(gate, monkeypatch, capsys):
    """Verify main() gates a Workflow payload and prints the deny.

    Mutation: dispatching only tool_name Agent.
    Oracle: the printed JSON is a deny naming the tier rule.
    """
    payload = workflow_input(META + "await agent('hi')")
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    gate.main()
    out = json.loads(capsys.readouterr().out)
    assert decision(out) == 'deny'
    assert 'name the tier' in reason(out)


def test_main_fails_open_on_a_workflow_error(gate, monkeypatch, capsys):
    """Verify a crash inside the Workflow decision is logged and allowed.

    Mutation: letting the exception propagate.
    Oracle: nothing printed and the log line says tool Workflow, error.
    """
    def explode(_hook_input):
        raise RuntimeError('boom')

    monkeypatch.setattr(gate, 'gate_workflow', explode)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(workflow_input(META))))
    gate.main()
    assert not capsys.readouterr().out
    line = last_log(gate)
    assert (line['tool'], line['decision']) == ('Workflow', 'error')
    assert 'boom' in line['reason']


def test_a_fable_stage_shares_the_seat_with_an_opus_xhigh_stage(gate):
    """Verify the two deriving tiers share one seat inside a script.

    Mutation: a per-tier seat counter, which would let a script carry one
        stage of each at opus-cap 6 and spend two derive seats where the
        cycle holds one.
    Oracle: an opus-xhigh stage and a fable-xhigh stage at opus-cap 6 are
        refused whole, naming one derive seat and #2, and the batch moves
        no counter.
    """
    calls = [
        stage(marked(opus_cap='6', derive='proof'), 'agent-scope:opus-xhigh'),
        stage(
            marked(opus_cap='6', tail='B', derive='joint-behavior'),
            'agent-scope:fable-xhigh'),
        ]
    out = run(gate, thunks(*calls))
    assert decision(out) == 'deny'
    assert 'holds 1 derive seat; this would be #2' in reason(out)
    assert cycle_state(gate) == EMPTY_CYCLE


def test_two_deriving_stages_of_different_tiers_fit_at_opus_cap_9(gate):
    """Verify a script may seat one stage of each deriving tier at 9.

    Mutation: admitting only opus-xhigh to the seat, so a fable-xhigh
        stage is refused even where the cycle has an unspent seat.
    Oracle: both stages allow, the systemMessage names seat 1/2 on the
        opus-xhigh line and seat 2/2 on the fable-xhigh line.
    """
    calls = [
        stage(marked(opus_cap='9', derive='proof'), 'agent-scope:opus-xhigh'),
        stage(
            marked(opus_cap='9', tail='B', derive='joint-behavior'),
            'agent-scope:fable-xhigh'),
        ]
    out = run(gate, thunks(*calls))
    assert decision(out) is None
    lines = out['systemMessage'].split('\n')
    assert any('opus-xhigh seat 1/2' in line for line in lines)
    assert any('fable-xhigh seat 2/2' in line for line in lines)


# --- Capped stages across the tiers and across tools ---


CAPPED_STAGE_TIERS = (
    'agent-scope:opus-medium',
    'agent-scope:opus-high',
    'agent-scope:opus-xhigh',
    'agent-scope:fable-xhigh',
    )
DERIVING_STAGE_ORDERS = [
    ('agent-scope:opus-xhigh', 'agent-scope:fable-xhigh'),
    ('agent-scope:fable-xhigh', 'agent-scope:opus-xhigh'),
    ]


@pytest.mark.parametrize('tier', CAPPED_STAGE_TIERS)
def test_every_capped_tier_is_held_to_the_stage_rules(gate, tier):
    """Verify the countable-position and literal-header rules cover all four.

    Mutation: testing either rule against the Opus tiers alone, so a
        fable-xhigh stage inside a mapped callback, or one whose prompt
        is built at runtime, runs unread and uncounted.
    Oracle: a stage inside a pipeline callback is denied for running
        more than once, a stage with a variable prompt is denied for
        hiding the header, and neither writes state.
    """
    mapped = 'await pipeline(args, d => ' + stage(marked(), tier) + ')'
    out = run(gate, mapped)
    assert decision(out) == 'deny'
    assert 'may run more than once' in reason(out)
    assert cycle_state(gate) is None
    out = run(gate, 'await ' + stage('args.prompt', tier))
    assert decision(out) == 'deny'
    assert 'opens its prompt with a string or template literal' in reason(out)
    assert cycle_state(gate) is None


@pytest.mark.parametrize(('first', 'second'), DERIVING_STAGE_ORDERS)
def test_derive_seats_are_shared_across_agent_and_workflow(gate, first, second):
    """Verify an Agent launch and a Workflow stage draw on one seat counter.

    Mutation: keeping a seat counter per tool, so a cycle at opus-cap 6
        seats one deriving Agent launch and one deriving stage.
    Oracle: the Agent launch takes the only seat; the script is then
        refused as derive seat #2 and leaves its round counter at 0.
    """
    launch = agent_input(header('review', '6', 'proof'), subagent_type=first)
    assert decision(gate.gate_agent(launch)) is None
    out = run(gate, 'await ' + stage(marked('verify', '6', derive='bound'), second))
    assert decision(out) == 'deny'
    assert 'holds 1 derive seat; this would be #2' in reason(out)
    assert cycle_state(gate)['xhigh'] == 1
    assert cycle_state(gate)['verify'] == 0


def test_a_mixed_batch_refused_on_the_seat_moves_no_counter(gate):
    """Verify a batch whose last stage overruns the seat commits nothing.

    Mutation: committing each stage's reservation as it is decided, so
        the cheap stage's log line and the two fitting capped stages
        persist while the batch is refused.
    Oracle: a haiku stage, an opus-high stage, and a seated opus-xhigh
        stage followed by a fable-xhigh stage at opus-cap 6 are refused
        whole as derive seat #2, leaving an empty cycle.
    """
    calls = [
        stage("'sweep the files'", 'agent-scope:haiku'),
        stage(marked(opus_cap='6'), 'agent-scope:opus-high'),
        stage(
            marked(opus_cap='6', tail='B', derive='proof'),
            'agent-scope:opus-xhigh'),
        stage(
            marked(opus_cap='6', tail='C', derive='bound'),
            'agent-scope:fable-xhigh'),
        ]
    out = run(gate, thunks(*calls))
    assert decision(out) == 'deny'
    assert 'holds 1 derive seat; this would be #2' in reason(out)
    assert cycle_state(gate) == EMPTY_CYCLE


def test_cheap_stages_are_outside_the_capped_quantity_rules(gate):
    """Verify repeated and mapped cheap stages pass beyond every threshold.

    Mutation: applying the round caps to cheap stages, or counting a
        mapped cheap stage once per tier rather than not at all; twelve
        marked cheap stages here would trip either.
    Oracle: a mapped sonnet stage plus twelve marked haiku stages in one
        script are allowed and write no state.
    """
    sweep = 'const rs = await pipeline(args, d => ' + stage(
        'd.prompt', 'agent-scope:sonnet-medium') + ')\n'
    calls = [
        stage(marked(gate.ROUNDS[i % 4], None, tail=str(i)), 'agent-scope:haiku')
        for i in range(12)
        ]
    assert run(gate, sweep + thunks(*calls)) is None
    assert cycle_state(gate) is None
