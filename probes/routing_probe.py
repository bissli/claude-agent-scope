#!/usr/bin/env python3
"""Ask a model to route labeled briefs from the directives it is given.

The plugin's directives and agent descriptions are instructions to a
model, so a test of them is a model reading them. This probe builds the
text a session sees - both directives and every agent description -
asks a model to restate the split test in its own words and to route a
fixed set of briefs, and scores the routing against the tier each brief
was written to land on. The restatement and the model's list of unclear
sentences print for a human to read; the routing decides the exit code.

Notes
-----
- Not part of the pytest suite: each model run is a paid call of about
  a quarter dollar and one to three minutes. Run it by hand after a
  wording change to the directives or the descriptions.
- Runs through headless Claude Code with the default system prompt
  replaced and user settings excluded, so the installed plugin's own
  session-start injection cannot reach the probe.
- A brief's expected tier is the one the taxonomy names for it; a
  mismatch means the text did not carry the rule to the reader, not
  that the reader is wrong.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
FRONTMATTER_RE = re.compile(r'\A---\n(?P<body>.*?)\n---\n', re.DOTALL)
DESCRIPTION_RE = re.compile(r'^description: >-\n(?P<block>(?:  .*\n)+)', re.MULTILINE)
TIERS = (
    'haiku', 'sonnet-medium', 'sonnet-high', 'opus-medium', 'opus-high',
    'opus-xhigh', 'fable-high', 'fable-xhigh')
BRIEFS = [
    ('a', 'agent-scope:opus-high',
     ('Review the three changed files of the auth refactor against the '
      'documented token contract and their callers; report what is wrong.')),
    ('b', 'agent-scope:fable-high',
     ('Decide whether a reader admitted after a write commits can be served '
      'stale state, across the four modules where A writes, B waits on a '
      'timeout, C caches the transformed row, and D invalidates on another '
      'path. The visibility contract and a reproducer harness are available.')),
    ('c', 'agent-scope:fable-xhigh',
     ('Establish that no interleaving of those four paths can expose stale '
      'state; a passing reproducer is not sufficient.')),
    ('d', 'agent-scope:opus-medium',
     ('Here are six claimed bugs, each with file and line and the invariant '
      'it breaks. Say which are real.')),
    ('e', 'agent-scope:opus-high',
     'This is a security-critical 40-file change. Review it very carefully.'),
    ('f', 'agent-scope:haiku',
     'List every caller of parse_header across the repo as file:line.'),
    ('g', 'agent-scope:sonnet-high',
     ('Implement the renamed flag described in this brief in cli.py and its '
      'two tests, and run the suite.')),
    ('h', 'agent-scope:fable-high',
     ('Here are the relevant lines of the four modules and the visibility '
      'contract. Decide whether the sequence A writes, B times out, C caches, '
      'D invalidates can serve stale state to a reader admitted after the '
      'commit.')),
    ]
QUESTIONS = (
    '===== QUESTIONS =====\n'
    'Q1. The directive says a brief may "fail to split". In your own words, in '
    'one or two sentences: what does that mean, and what concrete test would '
    'you apply to a brief to decide whether it fails to split?\n'
    'Q2. Route each brief below to exactly one subagent type, and quote the '
    'sentence of the directive or description that decides it:\n'
    '{briefs}\n'
    'Q3. List every sentence or phrase in the directives you found unclear or '
    'that could be read two ways, quoting it.\n\n'
    'Reply with JSON only, no prose outside the JSON and no prose after any '
    'string value, of this shape: {{"definition": str, "test": str, "routing": '
    '[{{"brief": str, "tier": str, "deciding_sentence": str}}], "unclear": '
    '[str]}}')
SYSTEM_PROMPT = 'You answer routing questions about a directive. Reply with JSON only.'


def main() -> int:
    """Run the probe for each requested model and return the exit code.

    Returns
    -------
    int
        0 when every brief routed to its expected tier on every model,
        else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--model', action='append', default=None,
        help='model alias to probe; repeatable; default opus')
    parser.add_argument(
        '--timeout', type=int, default=600, help='seconds per model run')
    parser.add_argument(
        '--raw', type=pathlib.Path, default=None,
        help='directory to save each raw reply as <model>.json')
    args = parser.parse_args()
    models = args.model or ['opus']

    directives = [
        (REPO / 'directives' / name).read_text(encoding='utf-8')
        for name in ('agent-model-selection.md', 'review-sizing.md')]
    descriptions = []
    for tier in TIERS:
        text = (REPO / 'agents' / f'{tier}.md').read_text(encoding='utf-8')
        block = DESCRIPTION_RE.search(FRONTMATTER_RE.match(text).group('body'))
        folded = ' '.join(line.strip() for line in block.group('block').splitlines())
        descriptions.append(f'agent-scope:{tier}: {folded}')
    brief_lines = '\n'.join(f' ({key}) "{text}"' for key, _, text in BRIEFS)
    prompt = (
        'You are an orchestrating agent in Claude Code. The text below is what '
        'you were given at session start, followed by the descriptions of the '
        'subagent types you may launch. Read them, then answer the questions.\n\n'
        '===== DIRECTIVES =====\n' + '\n'.join(directives)
        + '\n===== SUBAGENT DESCRIPTIONS =====\n' + '\n'.join(descriptions)
        + '\n' + QUESTIONS.format(briefs=brief_lines))

    env = {k: v for k, v in os.environ.items()
           if k not in {'CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION'}}
    failures = 0
    for model in models:
        print(f'===== {model} =====')
        run = subprocess.run(
            ['claude', '-p', '--model', model, '--setting-sources', 'project',
             '--system-prompt', SYSTEM_PROMPT, '--output-format', 'json',
             '--max-turns', '1'],
            input=prompt, capture_output=True, text=True, env=env,
            timeout=args.timeout)
        if run.returncode != 0:
            print(f'claude exited {run.returncode}: {run.stderr.strip()[:500]}')
            failures += 1
            continue
        envelope = json.loads(run.stdout)
        answer = envelope.get('result') or ''
        if args.raw is not None:
            args.raw.mkdir(parents=True, exist_ok=True)
            (args.raw / f'{model}.json').write_text(run.stdout, encoding='utf-8')
        print(f'cost ${envelope.get("total_cost_usd", 0):.2f}, '
              f'models {sorted(envelope.get("modelUsage", {}))}')
        # Notes:
        # - A model sometimes appends prose after a JSON string value,
        #   which breaks a strict parse; the regex fallback reads each
        #   field on its own so one stray clause does not void the run.
        start, end = answer.find('{'), answer.rfind('}')
        try:
            parsed = json.loads(answer[start:end + 1])
            if not isinstance(parsed, dict) or not parsed.get('routing'):
                raise json.JSONDecodeError('no routing list', answer, 0)
        except json.JSONDecodeError:
            parsed = {}
            for key in ('definition', 'test'):
                match = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', answer)
                parsed[key] = match.group(1) if match else ''
            parsed['routing'] = [
                {'brief': m.group(1), 'tier': m.group(2),
                 'deciding_sentence': m.group(3)}
                for m in re.finditer(
                    r'"brief"\s*:\s*"\W*(\w)\W*"\s*,\s*"tier"\s*:\s*"([^"]+)"\s*,\s*'
                    r'"deciding_sentence"\s*:\s*"((?:[^"\\]|\\.)*)"', answer)]
            parsed['unclear'] = re.findall(
                r'"((?:[^"\\]|\\.)*)"', answer[answer.find('"unclear"') + 9:])
            print('(strict JSON parse failed; fields read by regex)')
        # A model may label a brief "(a)" or "a." where the prompt wrote
        # "(a)"; the single letter is the key.
        routed = {}
        for entry in parsed.get('routing', []):
            letters = re.findall(r'[a-z]', str(entry.get('brief', '')).lower())
            if letters:
                routed[letters[0]] = entry
        for key, expected, _ in BRIEFS:
            entry = routed.get(key, {})
            got = str(entry.get('tier', '')).strip()
            verdict = 'PASS' if got == expected else 'FAIL'
            failures += verdict == 'FAIL'
            print(f'{verdict} ({key}) expected {expected}, got {got or "nothing"}')
            print(f'      decided by: {str(entry.get("deciding_sentence", ""))[:160]}')
        print(f'\nDEFINITION: {parsed.get("definition", "")}')
        print(f'TEST: {parsed.get("test", "")}')
        print('UNCLEAR:')
        for item in parsed.get('unclear', []):
            print(f' - {item}')
        print()
    print(f'{failures} failure(s)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
