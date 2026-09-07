"""The routing probe: a model reads the directives and routes and sizes as they say.

The plugin's directives and agent descriptions are instructions to a
model, so a test of them is a model reading them. This module builds the
text a session sees - both directives and every agent description - and
scores a model's answers to three questions: restate the split test,
route a fixed set of briefs, and size a fixed set of review rounds. Each
brief sits on one boundary the taxonomy draws and names the rule that
decides it; each sizing scenario has the cap and header review-sizing
prescribes.

The pure parse and score run with the suite. The model runs sit behind
the ``probe`` marker, which pytest.ini deselects by default; ``pytest -m
probe`` opts in, and each selected model is PROBE_RUNS paid calls of
under half a dollar and one to five minutes each. An item passes when a
majority of a model's runs route it as the directives say, since one run
is one sample of a model's routing.

Notes
-----
- A mismatch means the text did not carry the rule to the reader, not
  that the reader is wrong: an expected answer is one a careful reader
  of the directives lands on, and where the directives admit two
  answers both are listed.
- One model run is one sample: a model's routing varies between runs,
  so a regression is a count over several runs, never one miss.
"""

import collections
import json
import os
import pathlib
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
FRONTMATTER_RE = re.compile(r'\A---\n(?P<body>.*?)\n---\n', re.DOTALL)
DESCRIPTION_RE = re.compile(r'^description: >-\n(?P<block>(?:  .*\n)+)', re.MULTILINE)
TIERS = (
    'haiku', 'sonnet-medium', 'sonnet-high', 'opus-medium', 'opus-high',
    'opus-xhigh', 'fable-high', 'fable-xhigh')
INLINE = 'inline'
NONE_WORDS = {'', 'none', 'null', 'absent', 'omitted', 'no', 'n/a', '-'}
SYSTEM_PROMPT = 'You answer routing questions about a directive. Reply with JSON only.'

# Each brief: key, the tiers the directives admit, the derive kinds a
# deriving route may name (None elsewhere), the rule that decides it,
# and the brief text. INLINE means no agent launches.
BRIEFS = [
    ('a', ('agent-scope:opus-high',), None,
     ('high: an oracle outside the agent (the contract, the callers); opus: '
     'three files review one at a time, so the brief splits'),
     ('Review the three changed files of the auth refactor against the '
     'documented token contract and their callers; report what is wrong.')),
    ('b', ('agent-scope:fable-high',), None,
     ('high: the contract and a reproducer settle the claim; fable: the '
     'ordering across all four modules is the question, the code exceeds one '
      'Opus read, and any split loses the ordering'),
     ('Decide whether a reader admitted after a write commits can be served '
     'stale state across the four modules where A writes, B waits on a '
      'timeout, C caches the transformed row, and D invalidates on another '
      'path: more code than one Opus agent reads at once, and the answer lives '
      'in the ordering across all four. The visibility contract and a '
      'reproducer harness are available.')),
    ('c', ('agent-scope:fable-xhigh',), ('interleaving', 'proof'),
     ('xhigh: a claim over every interleaving that no reproducer settles; '
     'fable: the argument spans all four modules, more than one Opus read, '
      'and splits into parts that assume each other'),
     ('Establish that no interleaving of those four paths - more code than one '
     'Opus agent reads at once - can expose stale state; a passing reproducer '
      'is not sufficient.')),
    ('d', ('agent-scope:opus-medium',), None,
     ('a verdict on a claim is Opus or Fable; medium: the claims, the lines, '
     'and the invariant are handed over, so the agent adds nothing'),
     ('Here are six claimed bugs, each with file and line and the invariant '
     'it breaks. Say which are real.')),
    ('e', ('agent-scope:opus-high',), None,
     ('importance, file count, and subject matter never raise the tier; a '
     'review of independent files splits, so opus-high'),
     'This is a security-critical 40-file change. Review it very carefully.'),
    ('f', ('agent-scope:haiku', 'Explore'), None,
     'haiku looks up: grep fan-out; Explore is its read-only form',
     'List every caller of parse_header across the repo as file:line.'),
    ('g', ('agent-scope:sonnet-high',), None,
     'sonnet executes: code, tests, or edits from a brief',
     ('Implement the renamed flag described in this brief in cli.py and its '
      'two tests, and run the suite.')),
    ('h', ('agent-scope:fable-high',), None,
     ('medium shape (claims, lines, and the contract handed over) on material '
      'beyond one Opus read that no subset settles; Fable has no medium rung, '
      'so fable-high'),
     ('Here are four hundred claims, one per call site across the four '
      'modules, each with its line, and the contract every site must satisfy: '
      'more than one Opus agent reads at once, and no claim can be checked '
      'without the other sites in view. Check each claim against the contract '
      'and say which hold.')),
    ('i', ('agent-scope:opus-high', 'agent-scope:opus-medium'), None,
     ('a page fits one Opus read, so nothing needs splitting and Opus holds '
     'it whatever the coupling; the effort turns on how much the agent adds'),
     ('Here are the relevant lines of the four modules and the visibility '
     'contract, a page in all. Decide whether the sequence A writes, B times '
      'out, C caches, D invalidates can serve stale state to a reader admitted '
      'after the commit.')),
    ('j', ('agent-scope:sonnet-high',), None,
     'sonnet: a cause inside one module',
     ('test_parse_header fails on a header with a trailing space. The failure '
      'is inside parse_header in scripts/review-gate.py; find the cause and '
      'fix it.')),
    ('k', ('agent-scope:opus-high',), None,
     'opus judges a cause across files; high: the failing run is the oracle',
     ('The round counter resets mid-cycle. The cause spans the hook, the '
      'state file it writes, and the transcript reader that keys the cycle; '
      'a failing run reproduces it. Find the cause.')),
    ('l', ('agent-scope:sonnet-medium',), None,
     ('medium: the edit pattern and the files are handed over; sonnet: a bulk '
     'edit with the pattern given'),
     ('Rename FOO to BAR in these forty listed files, exactly as the attached '
     'pattern shows, and run the suite.')),
    ('m', ('agent-scope:haiku',), None,
     'haiku looks up: classification',
     ('Classify these three hundred log lines as error, warning, or info, and '
      'count each.')),
    ('n', ('agent-scope:opus-xhigh',), ('bound', 'formula'),
     ('xhigh: a bound over every session length that no sample settles; the '
     'function fits one Opus read, so opus, not fable'),
     ('Derive a bound on the state file size that holds for every session '
     'length, as a function of KEPT_CYCLES and the four rounds. A sample of '
      'sessions is a check, not the bound.')),
    ('o', ('agent-scope:opus-high',), None,
     ('high, not xhigh: the failing test is an oracle outside the agent, and '
     'a task that is merely hard or sensitive is high'),
     ('This concurrency bug is subtle and critical. Here is the failing test '
     'and the three files involved; find the cause.')),
    ('p', ('agent-scope:sonnet-high',), None,
     ('review coverage past the capped budget goes to sonnet-high, which '
      'returns claims, never verdicts'),
     ('The review round has spent its three Opus launches. Review these thirty '
      'further files for the same class of defect and report each finding as a '
      'candidate with file, line, and evidence; a later agent judges them.')),
    ('q', (INLINE,), None,
     'dispatch table: single-fact lookup, file known, is done inline',
     'What value does KEPT_CYCLES have in scripts/review-gate.py?'),
    ('r', (INLINE,), None,
     ('dispatch table: reasoning-critical work that needs the conversation '
     'stays inline'),
     ('Given everything discussed so far in this conversation, decide whether '
     'to take option B.')),
    ('s', ('Explore', 'agent-scope:haiku'), None,
     "Explore is haiku's read-only form when the file set is unknown",
     ('Find where the session state files are written; the file set is '
      'unknown.')),
    ('t', ('agent-scope:opus-xhigh',), ('equivalence',),
     ('xhigh: an equivalence no test distinguishes; both tokenizers fit one '
     'Opus read, so opus, not fable'),
     ('Establish that the new tokenizer and the old one accept exactly the '
     'same scripts; no test corpus distinguishes them, and both fit in one '
      'read.')),
    ('u', ('agent-scope:opus-high',), None,
     ('size is not the split test: independent modules split by module, so '
     'opus-high however many lines'),
     ('Review this 2,000-line refactor across 25 independent modules for '
     'defects; each module stands alone.')),
    ('v', ('agent-scope:opus-high',), None,
     'opus judges: a synthesis, with verdicts on the disagreements',
     ("Merge the three reviewers' reports into one ranked list, resolving "
      'their disagreements.')),
    ('w', ('agent-scope:haiku',), None,
     'haiku looks up: a summary of a log',
     ('Summarize this 5,000-line test log into the failing tests and the '
      'first error line of each.')),
    ('x', ('agent-scope:opus-xhigh',), ('proof',),
     ('xhigh: a proof no test settles; one 80-line function fits one Opus '
     'read, so opus-xhigh, not fable-xhigh'),
     ('Prove the loop invariant of this single 80-line function; nothing '
     'outside it matters.')),
    ]

# Each sizing scenario: key, the opus-caps the cycle may declare (None
# where no capped launch fixes one), the derive seats the scenario
# spends (None where the text does not ask), the header as
# allowed values per field (None in a set allows the field to be
# absent; None in place of the dict asks for no header), the rule, and
# the scenario text.
SIZINGS = [
    ('S1', {'3'}, None,
     {'round': {'review'}, 'opus-cap': {'3'}, 'derive': {None}},
     'up to 3 briefs, none deriving: cap 3; the header carries round and cap',
     ('You plan two opus-high review briefs and no derivation. Which opus-cap '
      'does the cycle declare, and what header opens the first launch?')),
    ('S2', {'6'}, {1},
     {'round': {'review'}, 'opus-cap': {'6'}, 'derive': {'proof'}},
     'one deriving brief declares 6 and spends one seat; derive names the kind',
     ('You plan three opus-high review briefs and one opus-xhigh brief that '
      'proves an invariant. Which opus-cap does the cycle declare, how many '
      'derive seats does it spend, and what header opens the opus-xhigh '
      'launch?')),
    ('S3', {'9'}, {2},
     {'round': {'review'}, 'opus-cap': {'9'}, 'derive': {'interleaving'}},
     ('two deriving briefs declare 9 and spend both seats, which the two '
     'deriving tiers share'),
     ('You plan one opus-xhigh brief proving an invariant and one fable-xhigh '
     'brief analyzing an interleaving, both in the review round. Which '
      'opus-cap does the cycle declare, how many derive seats does it spend, '
      'and what header opens the fable-xhigh launch?')),
    ('S4', {'3'}, None,
     {'round': {'verify'}, 'opus-cap': {'3'}, 'derive': {None}},
     ('a fixed cap carries into every round; verify with handed claims is '
     'opus-medium and takes no seat'),
     ('The review round fixed opus-cap 3. You now launch one opus-medium '
     'verify agent handed the surviving claims and their lines. Which '
      'opus-cap does the launch declare, and what is its header?')),
    ('S5', {'6', None}, None,
     {'round': {'synthesize'}, 'opus-cap': {'6', None}, 'derive': {None}},
     ('a synthesize launch needs no opus-cap; one it declares must match the '
     'fixed value'),
     ('The cycle is fixed at opus-cap 6 and the rounds have reported. You '
     'launch one opus-high synthesizer. What is its header?')),
    ('S6', {'3'}, None,
     {'round': {'swarm'}, 'opus-cap': {'3'}, 'derive': {None}},
     'capped work outside a review declares round: swarm and an opus-cap',
     ('Outside any review, you launch one opus-high agent to find a cause '
      'that spans three files. Nothing else capped runs this cycle. What '
      'header opens it, and which opus-cap does it declare?')),
    ('S7', {None}, None, None,
     'sonnet and haiku tiers are uncapped and fix nothing',
     ('You launch ten sonnet-high sweeps and nothing else. Which opus-cap '
      'does the cycle declare?')),
    ('S8', {'9'}, {2}, None,
     ('a cycle seats at most two derivations, at opus-cap 9; the rest merge '
     'or wait for the next prompt'),
     ('You have four deriving briefs. How many derive seats can one cycle '
     'hold, at which opus-cap, and what happens to the briefs past that?')),
    ('S9', {'3'}, {0},
     {'round': {'verify'}, 'opus-cap': {'3'}, 'derive': {None}},
     'fable-high is capped and takes no derive seat; it carries no derive',
     ('In a cycle fixed at opus-cap 3, you launch one fable-high verify agent '
      'on a brief that fails to split. Which opus-cap does it declare, how '
      'many derive seats does it spend, and what is its header?')),
    ('S10', {'6', '9'}, None,
     {'round': {'synthesize'}, 'opus-cap': {'6', '9', None},
      'derive': {'proof'}},
     ('a deriving synthesize launch needs a cap fixed by a review, verify, or '
     'swarm launch first, at 6 or 9, since 3 seats no derivation'),
     ('Nothing capped has run this cycle. You want one opus-xhigh synthesizer '
     'proving an invariant. Which opus-cap must the cycle declare first, and '
      'what header opens the synthesizer?')),
    ]

QUESTIONS = (
    '===== QUESTIONS =====\n'
    'Q1. The directive says a brief may "fail to split". In your own words, in '
    'one or two sentences: what does that mean, and what concrete test would '
    'you apply to a brief to decide whether it fails to split?\n'
    'Q2. Route each brief below. Name exactly one subagent type as the Agent '
    'tool would take it (for example agent-scope:opus-high, or Explore), or '
    'the word inline where the directive says to do the work yourself. On a '
    'deriving tier also name the derive kind. Quote the first eight words of '
    'the sentence of a directive or description that decides it.\n'
    '{briefs}\n'
    'Q3. Size each scenario below from the review-sizing directive. Give the '
    'opus-cap the cycle declares (3, 6, 9, or none), the derive seats the '
    'scenario spends where asked, and the header of the named launch as its '
    'three fields, with a field null where the header omits it.\n'
    '{sizings}\n'
    'Q4. List up to five sentences or phrases in the directives you found '
    'unclear or that could be read two ways, quoting each briefly.\n\n'
    'Reply with JSON only, no prose outside the JSON and no prose after any '
    'string value, of this shape: {{"definition": str, "test": str, '
    '"routing": [{{"brief": str, "tier": str, "derive": str or null, '
    '"deciding_sentence": str}}], "sizing": [{{"scenario": str, "opus_cap": '
    'str, "seats": int or null, "header": {{"round": str, "opus-cap": str or '
    'null, "derive": str or null}} or null}}], "unclear": [str]}}')

QUOTED = r'"((?:[^"\\]|\\.)*)"'
QUOTED_OR_NULL = r'(?:"((?:[^"\\]|\\.)*)"|null)'


def build_prompt(
    repo: pathlib.Path = REPO,
    briefs: list[tuple] = BRIEFS,
    sizings: list[tuple] = SIZINGS) -> str:
    """Return the probe prompt: both directives, every description, the questions.

    Parameters
    ----------
    repo : pathlib.Path
        The plugin repo root whose directives/ and agents/ are read.
    briefs : list[tuple]
        The BRIEFS rows to ask about; a shard passes a subset.
    sizings : list[tuple]
        The SIZINGS rows to ask about; a shard passes a subset.

    Returns
    -------
    str
        The prompt handed to the model on stdin.
    """
    directives = [
        (repo / 'directives' / name).read_text(encoding='utf-8')
        for name in ('agent-model-selection.md', 'review-sizing.md')]
    descriptions = []
    for tier in TIERS:
        text = (repo / 'agents' / f'{tier}.md').read_text(encoding='utf-8')
        block = DESCRIPTION_RE.search(FRONTMATTER_RE.match(text).group('body'))
        lines = block.group('block').splitlines()
        folded = ' '.join(line.strip() for line in lines)
        descriptions.append(f'agent-scope:{tier}: {folded}')
    brief_lines = '\n'.join(f' ({key}) "{text}"' for key, _, _, _, text in briefs)
    sizing_lines = '\n'.join(f' ({key}) "{text}"' for key, _, _, _, _, text in sizings)
    return (
        'You are an orchestrating agent in Claude Code. The text below is what '
        'you were given at session start, followed by the descriptions of the '
        'subagent types you may launch. Read them, then answer the questions.\n\n'
        '===== DIRECTIVES =====\n' + '\n'.join(directives)
        + '\n===== SUBAGENT DESCRIPTIONS =====\n' + '\n'.join(descriptions)
        + '\n' + QUESTIONS.format(briefs=brief_lines, sizings=sizing_lines))


def parse_answer(answer: str) -> dict[str, Any]:
    """Return the model's JSON reply as a dict, recovering from stray prose.

    Parameters
    ----------
    answer : str
        The model's whole reply.

    Returns
    -------
    dict[str, Any]
        The parsed reply. When the strict parse fails or carries no
        routing list, each field is read on its own by regex, so one
        stray clause after a string value does not void the run; the
        key 'lenient' is then True.

    Notes
    -----
    - A model sometimes appends prose after a JSON string value, which
      breaks a strict parse of the whole document but leaves every
      field readable on its own.
    """
    start, end = answer.find('{'), answer.rfind('}')
    try:
        parsed = json.loads(answer[start:end + 1])
        if isinstance(parsed, dict) and parsed.get('routing'):
            return parsed
    except json.JSONDecodeError:
        pass
    parsed = {'lenient': True}
    for key in ('definition', 'test'):
        match = re.search(rf'"{key}"\s*:\s*{QUOTED}', answer)
        parsed[key] = match.group(1) if match else ''
    route_re = (
        rf'"brief"\s*:\s*{QUOTED}\s*,\s*"tier"\s*:\s*{QUOTED}'
        rf'\s*(?:,\s*"derive"\s*:\s*{QUOTED_OR_NULL})?'
        rf'\s*(?:,\s*"deciding_sentence"\s*:\s*{QUOTED})?')
    parsed['routing'] = [
        {'brief': m.group(1), 'tier': m.group(2), 'derive': m.group(3),
         'deciding_sentence': m.group(4) or ''}
        for m in re.finditer(route_re, answer)]
    size_re = (
        rf'"scenario"\s*:\s*{QUOTED}\s*,\s*"opus_cap"\s*:\s*{QUOTED_OR_NULL}'
        r'(.*?)(?=\{\s*"scenario"|\]\s*,?\s*"unclear"|\Z)')
    parsed['sizing'] = []
    for m in re.finditer(size_re, answer, re.DOTALL):
        tail = m.group(3)
        seats = re.search(r'"seats"\s*:\s*(\d+|null)', tail)
        fields = {}
        for field in ('round', 'opus-cap', 'derive'):
            hit = re.search(rf'"{field}"\s*:\s*{QUOTED_OR_NULL}', tail)
            fields[field] = hit.group(1) if hit else None
        has_header = re.search(r'"header"\s*:\s*\{', tail) is not None
        seat_count = None
        if seats and seats.group(1) != 'null':
            seat_count = int(seats.group(1))
        parsed['sizing'].append({
            'scenario': m.group(1),
            'opus_cap': m.group(2),
            'seats': seat_count,
            'header': fields if has_header else None})
    unclear_at = answer.find('"unclear"')
    parsed['unclear'] = (
        re.findall(QUOTED, answer[unclear_at + 9:]) if unclear_at >= 0 else [])
    return parsed


def score(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Score one parsed reply against BRIEFS and SIZINGS.

    Parameters
    ----------
    parsed : dict[str, Any]
        A reply as parse_answer() returns it.

    Returns
    -------
    list[dict[str, Any]]
        One row per brief and per scenario: kind ('route' or 'size'),
        key, expected (a short text), got (a short text), passed
        (bool), and decided_by (the model's quoted sentence on a route).

    Notes
    -----
    - A brief is keyed by the first letter in its 'brief' field, since a
      model may write "(a)", "a." or the whole brief text there; a
      scenario by the first digits after an s.
    - A route passes when the tier is one the brief admits and, on a
      deriving route, the kind is one the brief admits; INLINE is a tier
      here.
    - A scenario passes when the opus-cap is one the scenario admits
      (none, null, and an empty string all meaning no cap), the seats
      match where the scenario asks, and each header field holds an
      allowed value, None allowing the field to be absent.
    """
    rows: list[dict[str, Any]] = []
    routed: dict[str, dict[str, Any]] = {}
    for entry in parsed.get('routing') or []:
        letters = re.findall(r'[a-z]', str(entry.get('brief', '')).lower())
        if letters:
            routed.setdefault(letters[0], entry)
    for key, tiers, derive, _, _ in BRIEFS:
        entry = routed.get(key, {})
        got_tier = str(entry.get('tier') or '').strip()
        raw_derive = str(entry.get('derive') or '').strip()
        got_derive = None if raw_derive.lower() in NONE_WORDS else raw_derive
        admitted = {tier.lower() for tier in tiers}
        tier_ok = got_tier.lower() in admitted
        derive_ok = derive is None or got_derive in derive
        expected = ' or '.join(tiers)
        if derive:
            expected += ' derive: ' + ' or '.join(derive)
        got = got_tier or 'nothing'
        if got_derive:
            got += f' derive: {got_derive}'
        rows.append({
            'kind': 'route',
            'key': key,
            'expected': expected,
            'got': got,
            'passed': tier_ok and derive_ok,
            'decided_by': str(entry.get('deciding_sentence') or '')})
    sized: dict[str, dict[str, Any]] = {}
    for entry in parsed.get('sizing') or []:
        match = re.search(r's\s*(\d+)', str(entry.get('scenario', '')).lower())
        if match:
            sized.setdefault(f'S{match.group(1)}', entry)
    for key, caps, seats, header, _, _ in SIZINGS:
        entry = sized.get(key, {})
        raw_cap = str(entry.get('opus_cap') or '').strip().lower()
        got_cap = None if raw_cap in NONE_WORDS else raw_cap
        got_seats = entry.get('seats')
        got_header = entry.get('header')
        if not isinstance(got_header, dict):
            got_header = {}
        header_ok = True
        if header is not None:
            for field, allowed in header.items():
                raw = str(got_header.get(field) or '').strip().lower()
                value = None if raw in NONE_WORDS else raw
                allowed_values = {v.lower() if v else None for v in allowed}
                header_ok = header_ok and value in allowed_values
        expected = 'cap ' + ' or '.join(sorted(c or 'none' for c in caps))
        if seats is not None:
            expected += ' seats ' + ' or '.join(str(s) for s in sorted(seats))
        if header is not None:
            expected += ' header ' + ' '.join(
                f'{field}={"|".join(sorted(v or "null" for v in allowed))}'
                for field, allowed in header.items())
        got = f'cap {got_cap or "none"}'
        if got_seats is not None:
            got += f' seats {got_seats}'
        if got_header:
            got += ' header ' + ' '.join(
                f'{field}={got_header.get(field) or "null"}'
                for field in ('round', 'opus-cap', 'derive'))
        seats_ok = seats is None or got_seats in seats
        rows.append({
            'kind': 'size',
            'key': key,
            'expected': expected,
            'got': got,
            'passed': got_cap in caps and seats_ok and header_ok,
            'decided_by': ''})
    return rows


def run_model(
    model: str,
    prompt: str,
    timeout: int = 600,
    raw_path: pathlib.Path | None = None) -> tuple[dict[str, Any] | None, str]:
    """Run one headless Claude Code call and return the parsed reply.

    Parameters
    ----------
    model : str
        The model alias for ``claude -p --model``.
    prompt : str
        The probe prompt, handed over on stdin.
    timeout : int, default 600
        Seconds to wait for the call.
    raw_path : pathlib.Path or None
        Where to save the raw JSON envelope, or None to keep nothing.

    Returns
    -------
    tuple[dict[str, Any] or None, str]
        The parsed reply, or None when the call failed, and a note: the
        call's cost, or the failure text.

    Notes
    -----
    - The default system prompt is replaced, user settings are excluded,
      and no MCP server starts, so the installed plugin's own
      session-start injection cannot reach the probe and the call spends
      its time on the answer; the two nesting variables are unset so the
      call runs from inside a Claude Code session.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in {'CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION'}}
    run = subprocess.run(
        ['claude', '-p', '--model', model, '--setting-sources', 'project',
         '--strict-mcp-config', '--system-prompt', SYSTEM_PROMPT,
         '--output-format', 'json', '--max-turns', '1'],
        input=prompt, capture_output=True, text=True, env=env, timeout=timeout)
    if run.returncode != 0:
        return None, f'claude exited {run.returncode}: {run.stderr.strip()[:500]}'
    if raw_path is not None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(run.stdout, encoding='utf-8')
    envelope = json.loads(run.stdout)
    cost = f'${envelope.get("total_cost_usd", 0):.2f}'
    return parse_answer(envelope.get('result') or ''), cost


PROBE_MODELS = os.environ.get('AGENT_SCOPE_PROBE_MODELS', 'opus,sonnet').split(',')
PROBE_RUNS = int(os.environ.get('AGENT_SCOPE_PROBE_RUNS', '3'))
PROBE_MIN_PASS = int(
    os.environ.get('AGENT_SCOPE_PROBE_MIN_PASS', str(PROBE_RUNS // 2 + 1)))
PROBE_RAW = os.environ.get('AGENT_SCOPE_PROBE_RAW')
PROBE_SHARDS = int(os.environ.get('AGENT_SCOPE_PROBE_SHARDS', '1'))


def test_parse_answer_reads_fields_past_prose_after_a_string():
    """Verify a reply with prose after a string value still yields its fields.

    Mutation: dropping the regex fallback, so a strict parse failure
        returns nothing and every item scores as unanswered.
    Oracle: a reply shaped like a real Sonnet answer, with a clause
        appended after one deciding_sentence, yields the brief, tier,
        cap, seats, and unclear values a strict parse of the clean reply
        gives.
    """
    clean = json.dumps({
        'definition': 'd', 'test': 't',
        'routing': [{'brief': 'a', 'tier': 'agent-scope:opus-high',
                     'derive': None, 'deciding_sentence': 'x'}],
        'sizing': [{'scenario': 'S7', 'opus_cap': 'none', 'seats': 0,
                    'header': None}],
        'unclear': ['u']})
    broken = clean.replace(
        '"deciding_sentence": "x"', '"deciding_sentence": "x" (see the high bullet)')
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    parsed = parse_answer(broken)
    assert parsed['lenient']
    assert parsed['routing'][0]['tier'] == 'agent-scope:opus-high'
    assert parsed['sizing'][0]['opus_cap'] == 'none'
    assert parsed['sizing'][0]['seats'] == 0
    assert parsed['sizing'][0]['header'] is None
    assert parsed['unclear'] == ['u']


def test_score_keys_a_brief_by_its_first_letter():
    """Verify a brief echoed as "(a) ..." or as its whole text is keyed a.

    Mutation: keying routing entries on the exact brief field, so a
        model that echoes the label in parentheses or repeats the brief
        text scores every item as unanswered.
    Oracle: two entries, one labeled "(a) Review ..." and one carrying
        the whole text of brief b, score as briefs a and b.
    """
    text_b = next(text for key, _, _, _, text in BRIEFS if key == 'b')
    rows = {row['key']: row for row in score({'routing': [
        {'brief': '(a) Review the three changed files',
         'tier': 'agent-scope:opus-high'},
        {'brief': f'(b) {text_b}', 'tier': 'agent-scope:fable-high'},
        ]})}
    assert rows['a']['passed']
    assert rows['b']['passed']


def test_score_requires_the_kind_on_a_deriving_route():
    """Verify a deriving route passes only with the kind the brief expects.

    Mutation: scoring the tier alone, so a derivation routed to the right
        tier under the wrong kind passes.
    Oracle: brief c on fable-xhigh with derive formula fails, since it
        admits interleaving or proof; brief n on opus-xhigh with derive
        bound passes.
    """
    rows = {row['key']: row for row in score({'routing': [
        {'brief': 'c', 'tier': 'agent-scope:fable-xhigh', 'derive': 'formula'},
        {'brief': 'n', 'tier': 'agent-scope:opus-xhigh', 'derive': 'bound'},
        ]})}
    assert not rows['c']['passed']
    assert rows['n']['passed']


def test_score_admits_each_listed_tier_and_no_other():
    """Verify alternates pass, other tiers fail, and inline is a tier.

    Mutation: comparing against the first listed tier only, which fails
        Explore on brief f; or treating inline as unanswered.
    Oracle: f admits Explore and rejects sonnet-medium; q admits inline
        and rejects haiku.
    """
    def passed(key, tier):
        rows = score({'routing': [{'brief': key, 'tier': tier}]})
        return next(row for row in rows if row['key'] == key)['passed']

    assert passed('f', 'Explore')
    assert not passed('f', 'agent-scope:sonnet-medium')
    assert passed('q', 'inline')
    assert not passed('q', 'agent-scope:haiku')


def test_score_reads_none_words_as_an_absent_cap_or_field():
    """Verify none, null, and an absent header field all read as absent.

    Mutation: comparing the literal string none to None, so scenario S7
        never passes; or requiring opus-cap on a synthesize header, which
        the directive lets the launch omit.
    Oracle: S7 with opus_cap none passes; S1 with derive null passes; S2
        without derive proof fails; S5 with opus-cap absent passes.
    """
    rows = {row['key']: row for row in score({'routing': [], 'sizing': [
        {'scenario': 'S7', 'opus_cap': 'none', 'seats': 0, 'header': None},
        {'scenario': 'S1', 'opus_cap': '3', 'seats': 0,
         'header': {'round': 'review', 'opus-cap': '3', 'derive': 'null'}},
        {'scenario': 'S2', 'opus_cap': '6', 'seats': 1,
         'header': {'round': 'review', 'opus-cap': '6', 'derive': None}},
        {'scenario': 'S5', 'opus_cap': '6', 'seats': 0,
         'header': {'round': 'synthesize', 'opus-cap': None, 'derive': None}},
        ]}) if row['kind'] == 'size'}
    assert rows['S7']['passed']
    assert rows['S1']['passed']
    assert not rows['S2']['passed']
    assert rows['S5']['passed']


def test_every_expected_answer_names_a_type_the_gate_accepts(gate):
    """Verify the briefs and scenarios expect only answers the gate admits.

    Mutation: a typo in an expected tier or kind, a brief routed to a
        deriving tier with no kind, a scenario expecting a cap outside
        3, 6, 9, or a duplicate key; each would fail every model for a
        reason that is not in the directives.
    Oracle: review_gate.TIERS, SEAT_TIERS, DERIVE_KINDS, CAPS, and
        ROUNDS, plus Explore and inline.
    """
    allowed = {f'{gate.TIER_PREFIX}{tier}' for tier in gate.TIERS}
    allowed |= {'Explore', INLINE}
    deriving = {f'{gate.TIER_PREFIX}{tier}' for tier in gate.SEAT_TIERS}
    keys = [key for key, *_ in BRIEFS]
    assert len(keys) == len(set(keys))
    assert set(TIERS) == set(gate.TIERS)
    for key, tiers, derive, _, _ in BRIEFS:
        assert set(tiers) <= allowed, key
        assert derive is None or set(derive) <= set(gate.DERIVE_KINDS), key
        assert (set(tiers) <= deriving) == (derive is not None), key
    keys = [key for key, *_ in SIZINGS]
    assert len(keys) == len(set(keys))
    for key, caps, seats, header, _, _ in SIZINGS:
        assert caps <= set(gate.CAPS) | {None}, key
        assert seats is None or seats <= {0, 1, 2}, key
        if header is not None:
            assert header['round'] <= set(gate.ROUNDS), key
            assert header['opus-cap'] <= set(gate.CAPS) | {None}, key
            assert header['derive'] <= set(gate.DERIVE_KINDS) | {None}, key


def test_the_prompt_carries_both_directives_and_every_description(gate):
    """Verify the prompt is the text a session sees, plus every item.

    Mutation: dropping a directive or a tier from the prompt builder, so
        the model routes from less than a session reads and a miss is
        blamed on the directives.
    Oracle: both directive files verbatim, one description line per gate
        tier, and one labeled line per brief and per scenario.
    """
    prompt = build_prompt()
    for name in ('agent-model-selection.md', 'review-sizing.md'):
        assert (REPO / 'directives' / name).read_text(encoding='utf-8') in prompt
    for tier in gate.TIERS:
        assert f'agent-scope:{tier}: ' in prompt
    for key, *_ in BRIEFS:
        assert f' ({key}) "' in prompt
    for key, *_ in SIZINGS:
        assert f' ({key}) "' in prompt


@pytest.fixture(scope='session')
def probe_results():
    """Run every probed model PROBE_RUNS times at once and return the replies.

    Returns
    -------
    dict[tuple[str, int], tuple[dict or None, str]]
        The parsed reply and its cost note per (model, run), as
        run_model() returns them.

    Notes
    -----
    - One pool holds every model's runs and shards, so the wall time is
      one call's, not the sum over models. The fixture is built on the
      first probe test that asks for it, so a run that deselects the
      marker never launches a call.
    - PROBE_SHARDS splits the briefs and scenarios round-robin across
      that many calls per run, each carrying the whole directive text:
      the wall time falls by less than that factor, since every call
      still reads the directives, and the input cost rises by it. The
      default of one call shows the model every item at once, the
      contrast set the recorded results were measured on.
    """
    raw_dir = pathlib.Path(PROBE_RAW) if PROBE_RAW else None
    shards = [
        build_prompt(briefs=BRIEFS[n::PROBE_SHARDS], sizings=SIZINGS[n::PROBE_SHARDS])
        for n in range(PROBE_SHARDS)]
    jobs = [
        (model, index, shard)
        for model in PROBE_MODELS
        for index in range(1, PROBE_RUNS + 1)
        for shard in range(PROBE_SHARDS)]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {
            job: pool.submit(
                run_model, job[0], shards[job[2]], 600,
                raw_dir / f'{job[0]}-{job[1]}-{job[2]}.json' if raw_dir else None)
            for job in jobs}
        replies = {job: future.result() for job, future in futures.items()}
    # Notes:
    # - A run's shards merge into one reply: the routing and sizing
    #   lists concatenate, the restatement and unclear list come from
    #   the first shard, and one failed shard fails the run.
    merged: dict[tuple[str, int], tuple[dict[str, Any] | None, str]] = {}
    for model in PROBE_MODELS:
        for index in range(1, PROBE_RUNS + 1):
            parts = [replies[(model, index, shard)] for shard in range(PROBE_SHARDS)]
            failed = [note for parsed, note in parts if parsed is None]
            if failed:
                merged[(model, index)] = (None, '; '.join(failed))
                continue
            whole = dict(parts[0][0])
            for field in ('routing', 'sizing'):
                whole[field] = [
                    entry for parsed, _ in parts for entry in parsed.get(field) or []]
            whole['lenient'] = any(parsed.get('lenient') for parsed, _ in parts)
            merged[(model, index)] = (whole, ' + '.join(note for _, note in parts))
    return merged


@pytest.mark.probe
@pytest.mark.parametrize('model', PROBE_MODELS)
def test_a_model_routes_and_sizes_as_the_directives_say(model, probe_results):
    """Verify a model reading the directives routes and sizes as they prescribe.

    Mutation: a directive edit that drops a rule - the split test, the
        importance guard, the medium and high line, the cap table - so
        the brief that rule decides routes elsewhere in most runs.
    Oracle: PROBE_RUNS live runs of the model on the exact text on disk,
        scored against the tier, kind, cap, and header the directives
        name for each item; an item passes when at least PROBE_MIN_PASS
        runs do, a majority by default. Every model's runs launch
        together through the probe_results fixture. The report prints
        under -s and on failure.
    """
    results = [probe_results[(model, index)] for index in range(1, PROBE_RUNS + 1)]
    passes = collections.Counter()
    wrong = collections.defaultdict(collections.Counter)
    report = [f'===== {model}: {PROBE_RUNS} run(s) =====']
    for index, (parsed, note) in enumerate(results, 1):
        assert parsed is not None, note
        report.append(f'run {index}: cost {note}'
                      + (' (fields read by regex)' if parsed.get('lenient') else ''))
        for row in score(parsed):
            if row['passed']:
                passes[row['key']] += 1
            else:
                wrong[row['key']][row['got']] += 1
        if index == 1:
            report += [
                f'  DEFINITION: {parsed.get("definition", "")}',
                f'  TEST: {parsed.get("test", "")}']
            report.extend(f'  UNCLEAR: {item}' for item in parsed.get('unclear') or [])
    items = []
    for key, tiers, derive, rule, _ in BRIEFS:
        expected = ' or '.join(tiers)
        if derive:
            expected += ' derive: ' + ' or '.join(derive)
        items.append((key, expected, rule))
    items += [(key, '', rule) for key, _, _, _, rule, _ in SIZINGS]
    failed = []
    for key, expected, rule in items:
        verdict = 'PASS' if passes[key] >= PROBE_MIN_PASS else 'FAIL'
        if verdict == 'FAIL':
            failed.append(key)
        line = f'{verdict} ({key}) {passes[key]}/{PROBE_RUNS}'
        if expected:
            line += f' expected {expected}'
        if wrong[key]:
            misses = wrong[key].most_common()
            line += ' | got ' + ', '.join(f'{got} x{n}' for got, n in misses)
        report.extend((line, f'      rule: {rule}'))
    print('\n'.join(report))
    assert not failed, '\n'.join(report)
