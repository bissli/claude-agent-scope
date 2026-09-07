#!/usr/bin/env python3
"""PreToolUse gate on Agent and Workflow: cap Opus and Fable launches.

A capped Agent launch opens its prompt with a header. The gate reads
that block and nothing else in the prompt:

    <review-gate>
    round: review|verify|synthesize|swarm
    opus-cap: 3|6|9
    derive: formula|bound|proof|equivalence|interleaving|joint-behavior
    </review-gate>

Notes
-----
- Every launch names its tier under the plugin prefix: subagent_type
  agent-scope:opus-medium, agent-scope:opus-high, agent-scope:opus-xhigh,
  agent-scope:fable-high, agent-scope:fable-xhigh, agent-scope:sonnet-medium,
  agent-scope:sonnet-high, or agent-scope:haiku, each an agent
  definition that pins its model and effort. A launch on
  general-purpose or with no type would inherit the session effort and
  is denied; so is a bare tier name, which names no agent the plugin
  ships, and a prefixed name that is not a tier.
- Only a capped launch takes a slot: an Opus or Fable tier, or a type
  that runs on the main-loop model. An explicit haiku or sonnet model,
  a sonnet or haiku tier, and an Explore agent that names no model sit
  outside the caps, in any quantity. Any other named type with no model
  is capped, and so is a fork whatever model it names: the runtime
  ignores a fork's model and runs it on the main-loop model.
- opus-cap is the ceiling on capped launches per round: 3, 6, or 9, one
  of three values and not a free number. The first counted review,
  verify, or swarm agent of a cycle fixes it; those three rounds then
  each hold that many capped launches. The synthesize round holds 2 per
  cycle at any opus-cap; a synthesize agent never fixes the value, and
  one it declares must match the fixed value. The counters are
  cumulative: a launch that finishes, fails, or dies frees no slot.
- A deriving agent, opus-xhigh or fable-xhigh, declares derive in every
  round, one of six kinds, naming what it must derive; a missing or
  unknown kind, or the field on any other tier, is denied before a slot
  is taken. The cycle's opus-cap sets its derive seats, none at 3, one
  at 6, two at 9, on one counter the two tiers share across the rounds;
  a launch past the seats is denied naming the high tiers and takes no
  slot. Two deriving briefs declare opus-cap 9, which seats both, so the
  tiers do not compete. A deriving synthesize agent is denied until a
  review, verify, or swarm agent has fixed the opus-cap, since the seat
  count reads off it and a synthesize agent cannot fix it. Refusals
  apply in the order mismatch, unfixed cycle, round cap, derive seat.
- A cycle is one user prompt. The key is the payload's prompt_id,
  unless the transcript shows that id stamped on a system record, a
  background task's completion re-entering the turn, in which case the
  key is the promptId of the last human record. With no prompt_id the
  key is that human record's promptId or uuid, else the uuid of the
  last user record from a CLI that stamps no origin. A launch whose
  cycle cannot be keyed is allowed and logged.
- A capped launch with no header is denied. Capped work outside a
  review declares round swarm, a fourth round counted and capped like
  review on its own counter. A cheap launch may omit the header, and a
  fable model option is always denied: an invocation-level model
  outranks the version pin in the Fable definitions' frontmatter, and
  the fable family alias is configurable and can change over time.
- Silence lets the call continue; a JSON deny blocks it. An allowed
  deriving launch prints a JSON systemMessage naming the seat, the
  kind, and the label; the user sees it and the call continues.
- A Workflow call is gated from its script text: every agent() stage is
  read as one Agent launch. The stage names its tier, prefix included
  and compared as written, as a literal agentType and carries no model
  or effort option; a capped stage opens its prompt with a literal
  carrying the header, and sits where it runs once: the top level, a
  thunk in parallel([...]), or a .then(), .catch(), or .finally()
  continuation. A loop, a pipeline stage, a mapped callback, or any
  other function around a capped stage
  is denied, as is a script that aliases or declares agent, binds
  parallel, defines then, calls workflow(), or names eval, Function,
  globalThis, constructor, import, or require. A saved workflow name is
  denied: the runtime resolves it by meta.name, so the gate cannot tell
  which script runs. The stages of one script reserve their slots
  together against the same cycle counters as Agent launches; a refused
  script takes no slot.
- Deterministic and standard library only; no model is launched. A crash
  inside the hook is logged and allows the call.
- Runs on Python 3.10 and later, on POSIX and Windows; the plugin wires
  it through sh, so the hook runs on Linux and macOS.
"""

import contextlib
import dataclasses
import json
import os
import pathlib
import re
import string
import sys
import time
from collections.abc import Iterator
from typing import Any, TextIO

# fcntl exists only on POSIX; Windows locks through msvcrt. The hook
# runs on both, so the import is resolved here rather than at the call.
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

CAPS = {'3': 3, '6': 6, '9': 9}
SYNTHESIZE_CAP = 2
XHIGH_SEATS = {'3': 0, '6': 1, '9': 2}
SEAT_KEY = 'xhigh'
DERIVE_KINDS = (
    'formula',
    'bound',
    'proof',
    'equivalence',
    'interleaving',
    'joint-behavior',
    )
KINDS_TEXT = ', '.join(DERIVE_KINDS)
OPUS_CAP_ROUNDS = ('review', 'verify', 'swarm')
ROUNDS = (*OPUS_CAP_ROUNDS, 'synthesize')
HEADER_FIELDS = {'round', 'opus-cap', 'derive'}
PLUGIN_NAME = 'agent-scope'
TIER_PREFIX = f'{PLUGIN_NAME}:'
OPUS_TIERS = ('opus-medium', 'opus-high', 'opus-xhigh')
FABLE_TIERS = ('fable-high', 'fable-xhigh')
CAPPED_TIERS = OPUS_TIERS + FABLE_TIERS
SEAT_TIERS = ('opus-xhigh', 'fable-xhigh')
CHEAP_TIERS = ('sonnet-medium', 'sonnet-high', 'haiku')
TIERS = CAPPED_TIERS + CHEAP_TIERS
SEAT_TIERS_TEXT = ' and '.join(TIER_PREFIX + tier for tier in SEAT_TIERS)
# The high tiers are offered together, with the condition that picks
# between them: without it a seat refusal reads as a push to Fable.
HIGH_TIERS_TEXT = (
    f'{TIER_PREFIX}opus-high, or {TIER_PREFIX}fable-high where the brief fails '
    'to split')
FABLE_TIERS_TEXT = ' or '.join(TIER_PREFIX + tier for tier in FABLE_TIERS)
TIERS_TEXT = ', '.join(TIER_PREFIX + tier for tier in TIERS)
INHERITING_TYPES = {'', 'general-purpose'}
FORK_TYPE = 'fork'
EXPLORE_TYPE = 'explore'
UNCAPPED_MODELS = {'haiku', 'sonnet'}
KEPT_CYCLES = 8
CHUNK_BYTES = 65_536
REASON_EXCERPT_CHARS = 80

STAGE_PINNED_OPTIONS = ('model', 'effort')
RESERVED_NAMES = ('eval', 'Function', 'globalThis', 'import', 'require', 'workflow')
LOOP_KEYWORDS = ('for', 'while', 'do')
FUNCTION_KEYWORDS = ('function', 'class')
CONDITION_KEYWORDS = ('if', 'switch', 'catch')
ONCE_CALLEES = ('parallel', 'agent', 'log', *CONDITION_KEYWORDS)
GROUPING_KEYWORDS = (
    'await', 'return', 'typeof', 'void', 'throw', 'yield', 'in', 'of', 'else',
    'case', 'delete', 'instanceof', 'new')
CONTINUATION_METHODS = ('then', 'catch', 'finally')
PROMISE_COMBINATORS = ('all', 'allSettled')
REGEX_PRECEDERS = {
    'return', 'typeof', 'case', 'do', 'else', 'in', 'of', 'new', 'delete', 'void',
    'throw', 'await', 'yield', 'instanceof'}
VALUE_CLOSERS = {')', ']', '}', '++', '--'}
BLOCK_PRECEDERS = {')', ';', '}', '{', '=>'}
BLOCK_KEYWORDS = {'else', 'try', 'finally', 'do', 'catch'}
CONTROL_HEADS = ('if', 'for', 'while', 'switch', 'catch', 'with')
NO_VALUE_NAMES = {
    *REGEX_PRECEDERS, 'const', 'let', 'var', 'if', 'for', 'while', 'switch',
    'function', 'class', 'async', 'try', 'finally', 'catch', 'export', 'default',
    'with'}
NON_STARTERS = ('in', 'instanceof', 'of', 'else', 'catch', 'finally')
CLAUSE_KEYWORDS = ('else', 'catch', 'finally')
TRUSTED_CALLS = ('parallel',)
DECLARATORS = ('function', 'const', 'let', 'var', 'class', 'async')
PUNCTUATORS = (
    '>>>=', '...', '===', '!==', '**=', '<<=', '>>=', '>>>', '=>', '==', '!=', '<=',
    '>=', '&&', '||', '??', '?.', '++', '--', '+=', '-=', '*=', '/=', '%=', '&=',
    '|=', '^=', '**', '<<', '>>')
BRACKET_PAIRS = {'(': ')', '[': ']', '{': '}', '${': '}'}
NAME_START = set(string.ascii_letters + '_$')
NAME_CHARS = NAME_START | set(string.digits)
JS_ESCAPES = {
    'n': '\n',
    't': '\t',
    'r': '\r',
    'b': '\b',
    'f': '\f',
    'v': '\v',
    '0': '\0',
    }
PASSING_HEADS = ONCE_CALLEES + GROUPING_KEYWORDS

HEADER_OPEN_RE = re.compile(r'\A\s*<review-gate\s*>', re.IGNORECASE)
HEADER_RE = re.compile(
    r'\A\s*<review-gate\s*>(?P<body>.*?)</review-gate\s*>[ \t]*(?:\r?\n|\Z)',
    re.IGNORECASE | re.DOTALL)
HEADER_FIELD_RE = re.compile(r'^\s*([a-z-]+)\s*:\s*(\S+)\s*$', re.IGNORECASE)
SESSION_NAME_RE = re.compile(r'[^A-Za-z0-9._-]')

STATE_HOME = pathlib.Path(
    os.environ.get('REVIEW_GATE_HOME', '~/.claude/cache/review-gate')).expanduser()
LOG_PATH = STATE_HOME / 'gate.jsonl'


def log_event(event: dict[str, Any]) -> None:
    """Append one line to gate.jsonl; a failed write never blocks a launch.
    """
    try:
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        payload = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), **event}
        with LOG_PATH.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(payload, ensure_ascii=True, default=str) + '\n')
    except OSError:
        pass


def deny(reason: str, event: dict[str, Any]) -> dict[str, Any]:
    """Log a denial and return the PreToolUse deny decision.

    Parameters
    ----------
    reason : str
        Text Claude Code returns to the model for the blocked call.
    event : dict[str, Any]
        Audit fields for the log line. The decision and reason are added.

    Returns
    -------
    dict[str, Any]
        The hookSpecificOutput envelope with permissionDecision deny.
    """
    log_event({**event, 'decision': 'deny', 'reason': reason})
    return {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
            },
        }


def allow(event: dict[str, Any]) -> None:
    """Log an allowed call. No hook output means the permission flow continues.
    """
    log_event({**event, 'decision': 'allow'})


def prompt_record(line: bytes) -> tuple[str, str | None, str | None] | None:
    """Classify one transcript line as a prompt record.

    Parameters
    ----------
    line : bytes
        One JSONL record from a Claude Code transcript.

    Returns
    -------
    tuple[str, str or None, str or None] or None
        (kind, promptId, uuid) for a user record with no tool_result
        block: 'human' when origin.kind is human, 'system' when it is
        any other value, 'legacy' when the record has no origin and is
        not meta. None for every other line, a meta record and a line
        that is not a JSON object included.

    Notes
    -----
    - The CLI stamps origin.kind on a user record: human for a typed or
      queued prompt, task-notification for a background task's
      completion, auto-continuation and peer for other re-entries. A
      command expansion or a hook message is a meta record under the
      prompt that raised it.
    """
    try:
        item = json.loads(line.decode('utf-8', 'replace'))
    except (TypeError, ValueError):
        return None
    if not isinstance(item, dict) or item.get('type') != 'user':
        return None
    message = item.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    blocks = content if isinstance(content, list) else []
    is_tool_result = any(
        isinstance(block, dict) and block.get('type') == 'tool_result'
        for block in blocks)
    if is_tool_result:
        return None
    prompt_id = str(item['promptId']) if item.get('promptId') else None
    uuid = str(item['uuid']) if item.get('uuid') else None
    origin = item.get('origin')
    if isinstance(origin, dict) and origin.get('kind'):
        kind = 'human' if origin['kind'] == 'human' else 'system'
        return kind, prompt_id, uuid
    if item.get('isMeta'):
        return None
    return 'legacy', prompt_id, uuid


def reversed_lines(transcript: str) -> Iterator[bytes]:
    """Yield a transcript's lines from the last to the first.

    Parameters
    ----------
    transcript : str
        Path to the session transcript named in the hook payload.

    Yields
    ------
    bytes
        One line without its newline. Nothing when the file cannot be
        opened.

    Notes
    -----
    - The scan reads the file backward in 64 KiB chunks and joins the
      pieces of a record only once its leading newline is found, so a
      long tail of tool output, or one huge record, costs a single pass
      over the bytes.
    """
    pending: list[bytes] = []
    with contextlib.suppress(OSError, ValueError), open(transcript, 'rb') as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        while position:
            chunk_size = min(CHUNK_BYTES, position)
            position -= chunk_size
            stream.seek(position)
            chunk = stream.read(chunk_size)
            if b'\n' not in chunk:
                pending.append(chunk)
                continue
            parts = (chunk + b''.join(reversed(pending))).split(b'\n')
            pending = [parts[0]]
            yield from reversed(parts[1:])
        yield b''.join(reversed(pending))


def turn_key(hook_input: dict[str, Any]) -> str | None:
    """Return the key that names one cycle.

    Parameters
    ----------
    hook_input : dict[str, Any]
        The PreToolUse payload.

    Returns
    -------
    str or None
        The payload's prompt_id, unless the transcript shows it stamped
        on a system record, in which case the promptId, else the uuid,
        of the last human record; with no prompt_id, that human key,
        else the uuid of the last user record that carries no origin;
        None when nothing resolves.

    Notes
    -----
    - The CLI mints a new prompt_id when a background task's completion
      re-enters the main loop, so the payload names the human prompt
      only until the first task notification of the turn. Keying on the
      human record behind the system one keeps the cycle to the prompt.
    - A prompt_id no system record carries is kept as it is: the human
      record may not have reached the file yet, and the payload is then
      the only witness to the turn.
    - A system or meta record never opens a cycle on either path.
    - The scan stops at the first human record, so a capped launch pays
      for the bytes since the current prompt; a cheap launch never
      reaches it.
    """
    prompt_id = str(hook_input['prompt_id']) if hook_input.get('prompt_id') else None
    human_key = legacy_uuid = None
    prompt_id_on_system = False
    for line in reversed_lines(str(hook_input.get('transcript_path') or '')):
        record = prompt_record(line)
        if record is None:
            continue
        kind, record_prompt_id, uuid = record
        if kind == 'human':
            human_key = record_prompt_id or uuid
            if human_key:
                break
        elif kind == 'system':
            if prompt_id is not None and record_prompt_id == prompt_id:
                prompt_id_on_system = True
        elif legacy_uuid is None:
            legacy_uuid = uuid
    if prompt_id and not prompt_id_on_system:
        return prompt_id
    return human_key or legacy_uuid or prompt_id


def parse_header(prompt: str) -> tuple[dict[str, str] | None, str | None]:
    """Parse the leading review-gate block of an Agent prompt.

    Parameters
    ----------
    prompt : str
        The full Agent prompt.

    Returns
    -------
    tuple[dict[str, str] or None, str or None]
        (fields, None) for a well-formed header, with keys and values
        lowercased; (None, None) when the prompt does not open with the
        block; (None, reason) when the block is malformed.

    Notes
    -----
    - Only a block at the very start of the prompt is control metadata.
      A header quoted later in the review text is never inspected.
    - The parser matches tags and field names case-insensitively and
      allows spaces inside the tags, so a near-miss such as
      `<review-gate >` is still a header rather than plain text. Any
      other tag, such as `<review-gate-x>`, is plain text.
    - Only a line feed, with or without a carriage return before it,
      ends a header line.
    """
    if not HEADER_OPEN_RE.match(prompt):
        return None, None
    match = HEADER_RE.match(prompt)
    if not match:
        return None, 'the leading <review-gate> header is not closed correctly'

    fields: dict[str, str] = {}
    for raw_line in match.group('body').split('\n'):
        line = raw_line.rstrip('\r')
        if not line.strip():
            continue
        field = HEADER_FIELD_RE.fullmatch(line)
        if not field:
            excerpt = line.strip()[:REASON_EXCERPT_CHARS]
            return None, f'invalid review-gate header line: {excerpt}'
        key, value = field.groups()
        key = key.lower()
        if key not in HEADER_FIELDS:
            return None, (
                f'unknown review-gate header field: {key}; the fields are '
                'round, opus-cap, and derive')
        if key in fields:
            return None, f'duplicate review-gate header field: {key}'
        fields[key] = value.lower()
    return fields, None


def lock_file(stream: TextIO, *, acquire: bool) -> None:
    """Take or release an exclusive lock on the open state file.

    Parameters
    ----------
    stream : TextIO
        The state file, open for reading and appending.
    acquire : bool
        True to lock, False to unlock.

    Notes
    -----
    - POSIX locks the whole file with flock. Windows locks the first byte
      with msvcrt, so the call rewinds the stream first to keep the
      locked region the same on both sides.
    """
    if fcntl is not None:
        fcntl.flock(stream, fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)
        return
    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def session_state(session_id: str) -> Iterator[dict[str, Any]]:
    """Yield the locked state dict for one session and write it back.

    Parameters
    ----------
    session_id : str
        The Claude Code session id. Characters outside [A-Za-z0-9._-]
        become '_' and the name is cut at 120 characters, so the file
        always lands inside STATE_HOME with an ASCII name.

    Yields
    ------
    dict[str, Any]
        The parsed state, or an empty dict when the file is new or holds
        anything but a JSON object. The context manager persists the
        dict on exit.
    """
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    safe_session = SESSION_NAME_RE.sub('_', session_id)[:120] or 'nosession'
    path = STATE_HOME / f'{safe_session}.json'
    with path.open('a+', encoding='utf-8') as stream:
        lock_file(stream, acquire=True)
        try:
            stream.seek(0)
            try:
                state = json.loads(stream.read() or '{}')
            except ValueError:
                state = {}
            if not isinstance(state, dict):
                state = {}
            yield state
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(state))
            stream.flush()
        finally:
            lock_file(stream, acquire=False)


def split_type(agent_type: str) -> tuple[bool, str]:
    """Split a launch's type into its plugin prefix and the name behind it.

    Parameters
    ----------
    agent_type : str
        The subagent_type of an Agent launch or the agentType of a
        Workflow stage, in the case the caller compares.

    Returns
    -------
    tuple[bool, str]
        Whether the type carries the ``agent-scope:`` prefix, and the
        name behind the prefix, or the whole type when it carries none.
    """
    if agent_type.startswith(TIER_PREFIX):
        return True, agent_type[len(TIER_PREFIX):]
    return False, agent_type


def is_uncapped(tool_input: dict[str, Any]) -> bool:
    """Return whether the Agent runs below the capped tiers.

    Parameters
    ----------
    tool_input : dict[str, Any]
        The Agent tool input as Claude Code sends it, before any hook
        rewrites it.

    Returns
    -------
    bool
        True for an explicit haiku or sonnet model, for a sonnet or haiku
        tier under the plugin prefix, and for an Explore agent that names
        no model; an uncapped launch is allowed in any quantity. An
        Explore agent with any other model is counted, because the pin
        hook strips an Opus alias and the launch then inherits the
        main-loop Opus model. A fork is counted whatever model it names:
        the runtime ignores a fork's model and runs it on the main-loop
        model. Type and model names compare lowercased.
    """
    model = str(tool_input.get('model') or '').lower()
    scoped, name = split_type(str(tool_input.get('subagent_type') or '').lower())
    if not scoped and name == FORK_TYPE:
        return False
    if model in UNCAPPED_MODELS or (scoped and name in CHEAP_TIERS):
        return True
    return not scoped and name == EXPLORE_TYPE and not model


@dataclasses.dataclass
class Reservation:
    """Outcome of one slot request.

    Attributes
    ----------
    allowed : bool
        Whether the launch took its slot.
    number : int
        The position this call would take on the counter that decided:
        the cycle's xhigh seat on a 'seat' refusal, else its capped
        slot.
    cap : int
        That counter's maximum: the cycle's seat count on a 'seat'
        refusal, else the round's cap on capped launches.
    fixed : str or None
        The cycle's opus-cap after this call, the declared string whose
        int is cap; None while no review, verify, or swarm agent has
        fixed one.
    refusal : str or None
        None when allowed, else the rule that refused: 'mismatch' (the
        declaration differs from fixed), 'unfixed' (a deriving
        synthesize agent before any opus-cap is fixed), 'cap' (the
        round's cap on capped launches), or 'seat' (the cycle's xhigh
        seats).
    seat : int or None
        On a deriving request against a fixed opus-cap, the seat this
        call took or would take; None otherwise.
    seats : int or None
        The cycle's seat count, XHIGH_SEATS[fixed], beside seat.
    """

    allowed: bool
    number: int
    cap: int
    fixed: str | None
    refusal: str | None = None
    seat: int | None = None
    seats: int | None = None


def reserve_slots(
    session_id: str,
    turn: str,
    requests: list[tuple[str, str | None, bool]]) -> list[Reservation]:
    """Atomically take capped slots in review rounds, all or none.

    Parameters
    ----------
    session_id : str
        Names the state file.
    turn : str
        The cycle key. Each cycle keeps its own counters and opus-cap.
    requests : list[tuple[str, str or None, bool]]
        One (round_name, opus_cap, xhigh) per launch, in launch order.
        round_name is 'review', 'verify', 'swarm', or 'synthesize', each
        with its own counter. opus_cap is the declared value, a key of
        CAPS, on a review, verify, or swarm agent; on a synthesize agent,
        None or a value to hold against the fixed opus-cap, which it
        never fixes.
        xhigh marks a deriving tier, opus-xhigh or fable-xhigh, which
        also takes one of the cycle's derive seats.

    Returns
    -------
    list[Reservation]
        One decision per request up to and including the first refusal.
        A refusal anywhere in the batch moves no counter: the state is
        written back as it was read, normalized.

    Notes
    -----
    - The review, verify, and swarm rounds each hold the cycle's opus-cap,
      fixed by the first agent of any of the three; CAPS is the membership
      check that turns the declared string into its int. The synthesize
      cap is SYNTHESIZE_CAP at any opus-cap; a synthesize agent never
      fixes the value, and one it declares while a value is fixed must
      match it. A deriving synthesize agent is refused while nothing
      is fixed, since it cannot fix the value itself.
    - The cycle holds XHIGH_SEATS[opus_cap] derive seats on one counter
      across the four rounds, which the deriving tiers share. Refusals
      apply in the order mismatch, unfixed, round cap, seat, so a seat
      refusal always names a repair the round has room for.
    - An Agent launch is a batch of one. A Workflow script is a batch of
      every marked capped stage it holds, so a script that would overrun
      a cap is refused whole and the corrected relaunch fits.
    - The state keeps the last KEPT_CYCLES cycles of the session, so
      launches from two cycles may interleave without resetting either.
    - A counter that is not a non-negative int, or an opus-cap outside
      CAPS, reads as empty. A damaged file is repaired on the next launch
      instead of failing open for the rest of the cycle.
    """
    results: list[Reservation] = []
    with session_state(session_id) as state:
        cycles = state.get('cycles')
        if not isinstance(cycles, dict):
            cycles = {}
        cycle = cycles.pop(turn, None)
        if not isinstance(cycle, dict):
            cycle = {}
        fixed_orig = cycle.get('opus_cap')
        if not isinstance(fixed_orig, str) or fixed_orig not in CAPS:
            fixed_orig = None
        counts_orig = {}
        for name in (*ROUNDS, SEAT_KEY):
            value = cycle.get(name)
            is_count = isinstance(value, int) and not isinstance(value, bool)
            counts_orig[name] = value if is_count and value >= 0 else 0

        fixed = fixed_orig
        counts = dict(counts_orig)
        for round_name, opus_cap, xhigh in requests:
            refusal = None
            if round_name == 'synthesize':
                cap = SYNTHESIZE_CAP
                if opus_cap is not None and fixed is not None and fixed != opus_cap:
                    refusal = 'mismatch'
                elif xhigh and fixed is None:
                    refusal = 'unfixed'
            else:
                fixed = fixed or opus_cap
                cap = CAPS[fixed]
                if fixed != opus_cap:
                    refusal = 'mismatch'
            proposed = counts[round_name] + 1
            seat = counts[SEAT_KEY] + 1 if xhigh and fixed is not None else None
            seats = XHIGH_SEATS[fixed] if seat is not None else None
            if refusal is None and proposed > cap:
                refusal = 'cap'
            elif refusal is None and seat is not None and seat > seats:
                refusal = 'seat'
            if refusal == 'seat':
                results.append(
                    Reservation(False, seat, seats, fixed, refusal, seat, seats))
            else:
                results.append(
                    Reservation(
                        refusal is None, proposed, cap, fixed, refusal, seat, seats))
            if refusal is not None:
                fixed, counts = fixed_orig, counts_orig
                break
            counts[round_name] = proposed
            if xhigh:
                counts[SEAT_KEY] = seat

        # The state contract shows the seat key only once a seat is
        # taken.
        stored = {name: n for name, n in counts.items() if n or name in ROUNDS}
        cycles[turn] = {'opus_cap': fixed, **stored}
        while len(cycles) > KEPT_CYCLES:
            del cycles[next(iter(cycles))]
        state.clear()
        state['cycles'] = cycles
    return results


def seat_line(seat_text: str, derive: str, label: str) -> str:
    """Return the systemMessage line for an allowed deriving launch.

    Parameters
    ----------
    seat_text : str
        The seat taken, or why the launch went uncounted.
    derive : str
        The declared kind.
    label : str
        The Agent description or the Workflow stage.

    Returns
    -------
    str
        One line the user sees at launch, naming seat, kind, and label.
    """
    return f'review-gate: {seat_text} - derive: {derive} - {label}'


def stage_label(site: dict[str, Any]) -> str:
    """Return a Workflow stage's line and label for its seat line.
    """
    return f'Workflow stage at line {site["line"]}, {site["label"] or "unlabeled"}'


def refusal_reason(slot: Reservation, round_name: str) -> str:
    """Return the deny text for a refused reservation.

    Parameters
    ----------
    slot : Reservation
        A reservation whose refusal is not None.
    round_name : str
        The round the launch declared.

    Returns
    -------
    str
        The reason, without the 'review-gate:' prefix, naming a repair
        that respects both the budget and the tier the brief needs.

    Notes
    -----
    - A cap already fixed cannot be raised, so a remedy never sends the
      caller back to declare a larger one for this cycle. Where the
      value is not yet fixed, the remedy names the value that seats a
      derivation, since the documented default of 3 seats none.
    - A high tier is offered for a deriving brief only where an oracle
      outside the agent can check it, fable-high where the brief fails to
      split. Where none can, the remedy is to merge the deriving briefs,
      and only while a seat remains to merge into: a cycle at opus-cap 3
      holds none, so there the derivation waits for the next prompt.
      Dropping derive to reach an exhausted allowance relabels the work
      rather than sizing it.
    """
    if slot.refusal == 'mismatch':
        return (
            f'this cycle was declared opus-cap {slot.fixed}; every capped launch '
            f'that declares opus-cap must use {slot.fixed}. The sonnet and haiku '
            'tiers are uncapped.')
    if slot.refusal == 'unfixed':
        return (
            'a deriving tier in the synthesize round needs a cycle whose opus-cap a '
            'review, verify, or swarm agent has fixed, since the seat count reads '
            'off it. Launch that round first declaring opus-cap 6 or 9, since 3 '
            'seats no derivation and a fixed cap does not rise; or, where an '
            f'oracle outside the agent can check this brief, use {HIGH_TIERS_TEXT}.')
    if slot.refusal == 'seat' and slot.cap == 0:
        return (
            f'a cycle at opus-cap {slot.fixed} holds no derive seat, and no later '
            'launch can raise a cap already fixed: a cycle carrying a deriving '
            'brief declares opus-cap 6 or 9 from its first capped launch. In this '
            'cycle, where an oracle outside the agent can check the brief, use '
            f'{HIGH_TIERS_TEXT}; where none can, the derivation waits for the '
            'next prompt, since every deriving launch of this cycle is refused '
            'here.')
    if slot.refusal == 'seat':
        return (
            f'a cycle at opus-cap {slot.fixed} holds {slot.cap} derive '
            f'seat{"s" if slot.cap != 1 else ""}; this '
            f'would be #{slot.number}. For a brief an oracle outside the agent can '
            f'check, use {HIGH_TIERS_TEXT}; otherwise merge the deriving briefs or '
            'hold one for the next prompt.')
    if round_name == 'synthesize':
        return (
            f'the synthesize round holds at most {slot.cap} capped agents per '
            f'cycle; this would be #{slot.number}. One synthesizer is the norm: '
            f'merge the remaining synthesis into it. {TIER_PREFIX}sonnet-high is '
            'uncapped for further coverage, and returns claims for that '
            'synthesizer to judge, never a verdict.')
    return (
        f'opus-cap {slot.fixed} allows at most {slot.cap} capped agents in the '
        f'{round_name} round; this would be #{slot.number}. A fixed cap does not '
        "rise, so merge overlapping briefs here and declare the cycle's whole "
        'capped and deriving demand from its first launch next time; extra '
        f'coverage takes {TIER_PREFIX}sonnet-high or {TIER_PREFIX}haiku, which are '
        'uncapped.')


def gate_agent(hook_input: dict[str, Any]) -> dict[str, Any] | None:
    """Decide one Agent launch.

    Parameters
    ----------
    hook_input : dict[str, Any]
        The PreToolUse payload for an Agent call.

    Returns
    -------
    dict[str, Any] or None
        A deny decision, or None to let the call continue.

    Notes
    -----
    - Order of checks: fable model, tier named under the prefix, header
      syntax, header present on a capped launch, round present and valid,
      opus-cap value, derive value and tier, uncapped tiers, derive
      present on a deriving tier, opus-cap presence on review, verify, and
      swarm, cycle key, then the slot reservation.
    - An allowed launch on either deriving tier returns a systemMessage
      envelope with no permission decision: the call continues and the
      user sees the seat, the kind, and the label.
    - Type names compare lowercased, as the model alias does; the log
      keeps the raw name.
    - Header syntax and field values are validated on every marked agent
      whatever its model, so ambiguous metadata never slips through on a
      cheap agent.
    """
    tool_input = hook_input.get('tool_input') or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    model = str(tool_input.get('model') or '').lower()
    agent_type = str(tool_input.get('subagent_type') or '')
    scoped, tier_name = split_type(agent_type.lower())
    event: dict[str, Any] = {
        'tool': 'Agent',
        'session': hook_input.get('session_id'),
        'model': model or None,
        'agent_type': agent_type or None,
        'label': tool_input.get('description'),
        }
    if model == 'fable':
        return deny(
            'review-gate: drop the fable model option; Fable is reached by '
            f'launching {FABLE_TIERS_TEXT} with no model option. An '
            "invocation-level model overrides the definitions' version pins, and "
            'the fable family alias is configurable and can change over time.',
            event)
    if not scoped and tier_name in INHERITING_TYPES:
        return deny(
            f'review-gate: name the tier: subagent_type {TIERS_TEXT}. '
            'general-purpose and an omitted type inherit the session effort.',
            event)
    if scoped and tier_name not in TIERS:
        return deny(
            f'review-gate: {agent_type} is not a tier; name one of {TIERS_TEXT}.',
            event)
    if not scoped and tier_name in TIERS:
        return deny(
            f"review-gate: the tiers are the {PLUGIN_NAME} plugin's agents; name "
            f'{TIER_PREFIX}{tier_name}, not {agent_type}.',
            event)

    header, header_error = parse_header(str(tool_input.get('prompt') or ''))
    if header_error:
        return deny(f'review-gate: {header_error}.', event)
    if header is None:
        if is_uncapped(tool_input):
            allow({**event, 'scope': 'not-review-marked'})
            return None
        return deny(
            'review-gate: a capped launch opens its prompt with a <review-gate> '
            'header; capped work outside a review declares round: swarm and an '
            'opus-cap.',
            event)

    round_name = header.get('round')
    if round_name not in ROUNDS:
        return deny(
            'review-gate: round must be review, verify, synthesize, or swarm.',
            {**event, 'round': round_name})
    event['round'] = round_name

    opus_cap = header.get('opus-cap')
    if opus_cap is not None and opus_cap not in CAPS:
        return deny(
            'review-gate: invalid opus-cap. Use 3, 6, or 9; the cap is one of three '
            'values, not a free number.',
            {**event, 'opus_cap': opus_cap})
    derive = header.get('derive')
    if derive is not None and derive not in DERIVE_KINDS:
        return deny(
            f'review-gate: invalid derive: {derive}. The kinds are {KINDS_TEXT}; a '
            'kind names what the agent must derive, and importance, breadth, and '
            'subject matter are not kinds. Where none fits the brief, use '
            f'{HIGH_TIERS_TEXT}.',
            {**event, 'derive': derive})
    if derive is not None and tier_name not in SEAT_TIERS:
        return deny(
            f'review-gate: derive belongs on a deriving tier; {agent_type} declared '
            f'derive: {derive}. Drop the field, or launch the deriving brief on '
            f'{SEAT_TIERS_TEXT}.',
            {**event, 'derive': derive})
    if is_uncapped(tool_input):
        allow({**event, 'scope': 'uncapped'})
        return None
    takes_seat = tier_name in SEAT_TIERS
    if takes_seat and derive is None:
        return deny(
            f'review-gate: {agent_type} needs derive: <kind> in the header, one of '
            f'{KINDS_TEXT}. Where an oracle outside the agent checks the result - a '
            f'spec, a schema, a test run, the callers - use {HIGH_TIERS_TEXT}.',
            {**event, 'opus_cap': opus_cap})
    if round_name in OPUS_CAP_ROUNDS and opus_cap is None:
        return deny(
            'review-gate: capped review, verify, and swarm agents require '
            'opus-cap: 3, 6, or 9 in the leading header.',
            event)

    if takes_seat:
        event['derive'] = derive
    label = str(event['label'] or 'unlabeled')
    turn = turn_key(hook_input)
    if turn is None:
        allow({**event, 'opus_cap': opus_cap, 'scope': 'no-turn'})
        if takes_seat:
            return {'systemMessage': seat_line(
                f'{tier_name} uncounted, no cycle key', derive, label)}
        return None
    session_id = str(hook_input.get('session_id') or 'nosession')
    slot = reserve_slots(session_id, turn, [(round_name, opus_cap, takes_seat)])[0]
    event.update({'turn': turn, 'opus_cap': opus_cap})
    if slot.refusal != 'seat':
        event.update({'round_n': slot.number, 'round_cap': slot.cap})
    if slot.seat is not None:
        event.update({'seat_n': slot.seat, 'seat_cap': slot.seats})
    if slot.refusal is None:
        allow(event)
        if takes_seat:
            return {'systemMessage': seat_line(
                f'{tier_name} seat {slot.seat}/{slot.seats}', derive, label)}
        return None
    event['refusal'] = slot.refusal
    if slot.refusal == 'mismatch':
        event['fixed'] = slot.fixed
    return deny(f'review-gate: {refusal_reason(slot, round_name)}', event)


class ScriptError(Exception):
    """A Workflow script the gate cannot accept. The message names the line.
    """


@dataclasses.dataclass
class Token:
    """One significant token of a Workflow script.

    Attributes
    ----------
    kind : str
        'name', 'number', 'punct', 'string', 'template', 'chunk', or
        'regex'. A template literal is one 'template' token for its
        leading static text, then '${' and '}' punctuators around each
        substitution and a 'chunk' for the static text after one.
    text : str
        The raw source text; for 'template' and 'chunk', the static text.
    line : int
        The 1-based line the token starts on.
    value : str or None
        The cooked text of a 'string', 'template', or 'chunk'; None for
        the other kinds.
    static : bool
        For 'template', whether the literal has no substitution.
    """

    kind: str
    text: str
    line: int
    value: str | None = None
    static: bool = True


def cook(raw: str, line: int) -> str:
    """Decode the escape sequences of a JavaScript string or template body.

    Parameters
    ----------
    raw : str
        The source text between the quotes, escapes intact.
    line : int
        The line the literal starts on, for the error message.

    Returns
    -------
    str
        The text the runtime sees, so an escaped header or tier name
        reads the same as a plain one.
    """
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        i += 1
        if ch != '\\':
            out.append(ch)
            continue
        if i >= len(raw):
            raise ScriptError(f'a literal at line {line} ends in a backslash')
        esc = raw[i]
        i += 1
        try:
            if esc in JS_ESCAPES:
                out.append(JS_ESCAPES[esc])
            elif esc == 'x':
                out.append(chr(int(raw[i:i + 2], 16)))
                i += 2
            elif esc == 'u' and raw[i:i + 1] == '{':
                end = raw.index('}', i)
                out.append(chr(int(raw[i + 1:end], 16)))
                i = end + 1
            elif esc == 'u':
                out.append(chr(int(raw[i:i + 4], 16)))
                i += 4
            elif esc == '\r':
                if raw[i:i + 1] == '\n':
                    i += 1
            elif esc != '\n':
                out.append(esc)
        except (ValueError, OverflowError) as exc:
            raise ScriptError(f'a bad escape in a literal at line {line}') from exc
    return ''.join(out)


def tokenize(script: str) -> list[Token]:
    r"""Split a Workflow script into significant tokens.

    Parameters
    ----------
    script : str
        The script text.

    Returns
    -------
    list[Token]
        Comments and whitespace dropped; each string, template chunk,
        and regex literal one token, with a template's substitutions
        tokenized in place between '${' and '}'.

    Notes
    -----
    - A '/' opens a regex literal after an operator, an opening bracket,
      or a keyword such as return; after a value it divides. A regex
      misread as division desynchronizes the scan, which then fails on
      an unterminated literal or an unbalanced bracket, so the script is
      denied rather than misread.
    - A backslash outside a literal is denied: an identifier escape such
      as \\u0061gent spells agent without the scan seeing the name.
    """
    tokens: list[Token] = []
    template_depths: list[int] = []
    depth = 0
    i = 0
    line = 1
    size = len(script)

    def scan_chunk(start: int) -> tuple[str, int, bool]:
        """Return a template's static text from start, the index after it,
        and whether it ended the literal rather than opening '${'.
        """
        j = start
        while j < size:
            if script[j] == '\\':
                j += 2
            elif script[j] == '`':
                return script[start:j], j + 1, True
            elif script.startswith('${', j):
                return script[start:j], j + 2, False
            else:
                j += 1
        raise ScriptError(f'an unterminated template literal at line {line}')

    while i < size:
        ch = script[i]
        closes_template = (
            ch == '}' and bool(template_depths) and depth == template_depths[-1])
        if ch == '\n':
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif script.startswith('//', i):
            end = script.find('\n', i)
            i = size if end < 0 else end
        elif script.startswith('/*', i):
            end = script.find('*/', i + 2)
            if end < 0:
                raise ScriptError(f'an unterminated comment at line {line}')
            line += script.count('\n', i, end)
            i = end + 2
        elif ch in '\'"':
            j = i + 1
            while j < size and script[j] != ch:
                if script[j] == '\n':
                    raise ScriptError(f'an unterminated string at line {line}')
                j += 2 if script[j] == '\\' else 1
            if j >= size:
                raise ScriptError(f'an unterminated string at line {line}')
            cooked = cook(script[i + 1:j], line)
            tokens.append(Token('string', script[i:j + 1], line, cooked))
            i = j + 1
        elif ch == '`' or closes_template:
            if closes_template:
                tokens.append(Token('punct', '}', line))
                depth -= 1
                template_depths.pop()
            text, i, closed = scan_chunk(i + 1)
            kind = 'template' if ch == '`' else 'chunk'
            tokens.append(Token(kind, text, line, cook(text, line), closed))
            line += text.count('\n')
            if not closed:
                tokens.append(Token('punct', '${', line))
                depth += 1
                template_depths.append(depth)
        elif ch in NAME_START:
            j = i + 1
            while j < size and script[j] in NAME_CHARS:
                j += 1
            tokens.append(Token('name', script[i:j], line))
            i = j
        elif ch.isdigit() or (ch == '.' and script[i + 1:i + 2].isdigit()):
            j = i + 1
            while j < size and (script[j] in NAME_CHARS or script[j] == '.'):
                j += 1
            tokens.append(Token('number', script[i:j], line))
            i = j
        elif ch == '\\':
            raise ScriptError(f'a backslash outside a literal at line {line}')
        elif ch == '/' and (
            not tokens
            or (tokens[-1].kind == 'punct' and tokens[-1].text not in VALUE_CLOSERS)
                or (tokens[-1].kind == 'name' and tokens[-1].text in REGEX_PRECEDERS)):
            j = i + 1
            in_class = False
            while j < size and (in_class or script[j] != '/'):
                if script[j] == '\n':
                    raise ScriptError(f'an unterminated regex at line {line}')
                if script[j] == '\\':
                    j += 1
                elif script[j] == '[':
                    in_class = True
                elif script[j] == ']':
                    in_class = False
                j += 1
            if j >= size:
                raise ScriptError(f'an unterminated regex at line {line}')
            j += 1
            while j < size and script[j] in NAME_CHARS:
                j += 1
            tokens.append(Token('regex', script[i:j], line))
            i = j
        else:
            text = next((p for p in PUNCTUATORS if script.startswith(p, i)), ch)
            tokens.append(Token('punct', text, line))
            if text == '{':
                depth += 1
            elif text == '}':
                depth -= 1
            i += len(text)
    if template_depths:
        raise ScriptError(f'an unterminated template literal at line {line}')
    return tokens


def match_brackets(
    tokens: list[Token]) -> tuple[dict[int, int], list[tuple[int, ...]]]:
    """Pair every bracket token and record what encloses each token.

    Parameters
    ----------
    tokens : list[Token]
        The tokenized script.

    Returns
    -------
    tuple[dict[int, int], list[tuple[int, ...]]]
        partner maps each opener index to its closer and back. enclosing
        holds, per token, the indices of the openers around it, outermost
        first; an opener and its closer share their parent's tuple.
    """
    partner: dict[int, int] = {}
    enclosing: list[tuple[int, ...]] = []
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind == 'punct' and token.text in BRACKET_PAIRS:
            enclosing.append(tuple(stack))
            stack.append(index)
        elif token.kind == 'punct' and token.text in {')', ']', '}'}:
            if not stack or BRACKET_PAIRS[tokens[stack[-1]].text] != token.text:
                raise ScriptError(f'an unmatched {token.text} at line {token.line}')
            opener = stack.pop()
            partner[opener] = index
            partner[index] = opener
            enclosing.append(tuple(stack))
        else:
            enclosing.append(tuple(stack))
    if stack:
        unclosed = tokens[stack[-1]]
        raise ScriptError(f'an unclosed {unclosed.text} at line {unclosed.line}')
    return partner, enclosing


def object_entries(
    tokens: list[Token],
    partner: dict[int, int],
    enclosing: list[tuple[int, ...]],
    opener: int,
    where: str) -> dict[str, list[int]]:
    """Return the key: value entries of an object literal.

    Parameters
    ----------
    tokens : list[Token]
        The tokenized script.
    partner : dict[int, int]
        Bracket pairs from match_brackets.
    enclosing : list[tuple[int, ...]]
        Enclosing openers from match_brackets.
    opener : int
        The index of the literal's '{'.
    where : str
        What the literal is, for the error message.

    Returns
    -------
    dict[str, list[int]]
        Each key, as the runtime reads it, to the indices of its value
        tokens. A spread, a computed or shorthand key, a method, or a
        repeated key raises ScriptError: the gate reads only a literal
        whose keys and values it can see.
    """
    inside = (*enclosing[opener], opener)
    entries: dict[str, list[int]] = {}
    current: list[int] = []
    closer = partner[opener]
    for index in range(opener + 1, closer + 1):
        token = tokens[index]
        at_depth = enclosing[index] == inside
        is_comma = at_depth and token.kind == 'punct' and token.text == ','
        if index != closer and not is_comma:
            current.append(index)
            continue
        if not current:
            continue
        key = tokens[current[0]]
        fault = f'{where} at line {key.line} has an entry that is not key: value'
        if key.kind == 'name':
            name = key.text
        elif key.kind == 'string' or (key.kind == 'template' and key.static):
            name = key.value or ''
        else:
            raise ScriptError(fault)
        colon = tokens[current[1]] if len(current) > 2 else None
        if colon is None or colon.kind != 'punct' or colon.text != ':':
            raise ScriptError(fault)
        if name in entries:
            raise ScriptError(f'{where} at line {key.line} repeats {name}')
        entries[name] = current[2:]
        current = []
    return entries


def multiplier(
    tokens: list[Token],
    partner: dict[int, int],
    enclosing: list[tuple[int, ...]],
    index: int) -> str | None:
    """Return why the call at index may run more than once, or None.

    Parameters
    ----------
    tokens : list[Token]
        The tokenized script.
    partner : dict[int, int]
        Bracket pairs from match_brackets.
    enclosing : list[tuple[int, ...]]
        Enclosing openers from match_brackets.
    index : int
        The index of the agent name token.

    Returns
    -------
    str or None
        A phrase naming the construct and its line, or None when every
        construct around the call runs its body at most once.

    Notes
    -----
    - The walk climbs from the call to the top level. At each level it
      reads the statement, or the element, holding the inner construct:
      a loop or function keyword there is a multiplier, and so is an
      arrow outside the two accepted places. It then reads the level's
      own opener: a call the gate does not know, a loop head, or a
      function body is a multiplier.
    - An arrow is accepted as an element of the array literal that is
      the sole argument of parallel(), and as an argument of .then(),
      .catch(), or .finally(). A call is accepted to parallel, agent,
      log, Promise.all, Promise.allSettled, and the heads of if, switch,
      and catch; a keyword such as await or return before '(' groups.
    - Inside braces a statement runs back to ';' or to the '}' of a
      block, except that a ';' or '}' followed by else, catch, or finally
      is an arm of the same statement, and an object literal's or a
      template substitution's '}' is skipped. A line break after a value
      and before a name, a number, or a string ends the statement, as
      automatic semicolon insertion does; a ')' closing the head of if,
      for, while, switch, catch, or with ends no value. The while that
      closes a do block ends that statement. Inside parentheses,
      brackets, and template substitutions an element runs back to ','
      or ';'. A comma never bounds a statement, so a loop head before a
      comma operator still counts.
    - A '(' whose ')' is followed by '=>' is a parameter list: a default
      value there runs on every call of the function.
    """
    chain = enclosing[index]
    position = index
    for level in range(len(chain), -1, -1):
        opener = chain[level - 1] if level else None
        stack = chain[:level]
        start = opener + 1 if opener is not None else 0
        in_braces = opener is None or tokens[opener].text == '{'
        segment: list[int] = []
        following = position
        j = position - 1
        while j >= start:
            if enclosing[j] != stack:
                j -= 1
                continue
            token = tokens[j]
            after = tokens[following]
            # Notes:
            # - Automatic semicolon insertion: a value, then a line
            #   break, then a token no expression or clause can continue
            #   with (a name, a number, a string) ends the statement.
            # - A ')' ends a value unless it closes the head of if, for,
            #   while, switch, catch, or with, whose body follows it.
            starts_statement = after.line > token.line and (
                after.kind in {'number', 'string'}
                or (after.kind == 'name' and after.text not in NON_STARTERS))
            if starts_statement:
                ends_value = (
                    token.kind in {'number', 'string', 'regex'}
                    or (token.kind in {'template', 'chunk'} and token.static)
                    or (token.kind == 'name' and token.text not in NO_VALUE_NAMES)
                    or (token.kind == 'punct' and token.text in {']', '}'}))
                if token.kind == 'punct' and token.text == ')':
                    head = tokens[partner[j] - 1] if partner[j] else None
                    ends_value = not (
                        head is not None and head.kind == 'name'
                        and head.text in CONTROL_HEADS)
                if ends_value:
                    break
            continues = after.kind == 'name' and after.text in CLAUSE_KEYWORDS
            if token.kind == 'punct' and token.text == ';':
                # The ';' that ends an if arm sits inside the statement.
                if not continues:
                    break
                j -= 1
                continue
            if token.kind == 'punct' and token.text == ',' and not in_braces:
                break
            if token.kind == 'punct' and token.text == '}':
                # Notes:
                # - A block ends the statement before it, unless else,
                #   catch, or finally follows: the block is an arm of
                #   the same statement.
                # - An object literal or a template substitution is part
                #   of the statement: skip it and keep reading.
                group = partner[j]
                prev = tokens[group - 1] if group else None
                is_block = tokens[group].text == '{' and (
                    prev is None
                    or (prev.kind == 'punct' and prev.text in BLOCK_PRECEDERS)
                    or (prev.kind == 'name' and prev.text in BLOCK_KEYWORDS))
                if is_block and in_braces and not continues:
                    break
                following = group
                j = group - 1
                continue
            if token.kind == 'name' and token.text == 'while' and j:
                # The while of a do loop closes that statement.
                tail = tokens[j - 1]
                is_tail = tail.kind == 'punct' and tail.text == '}' and partner[j - 1]
                do_head = tokens[partner[j - 1] - 1] if is_tail else None
                if do_head is not None and do_head.kind == 'name' and (
                        do_head.text == 'do'):
                    break
            segment.append(j)
            following = j
            j -= 1
        for j in segment:
            token = tokens[j]
            if token.kind == 'name' and token.text in LOOP_KEYWORDS:
                return f'a {token.text} loop at line {token.line}'
            if token.kind == 'name' and token.text in FUNCTION_KEYWORDS:
                return f'a {token.text} at line {token.line}'
        arrows = [
            j for j in segment
            if tokens[j].kind == 'punct' and tokens[j].text == '=>']
        if arrows:
            placed = False
            if opener is not None and tokens[opener].text == '[' and opener >= 2:
                closer = partner[opener]
                after_index = closer + 1
                if after_index < len(tokens) and tokens[after_index].text == ',':
                    after_index += 1
                after = tokens[after_index] if after_index < len(tokens) else None
                paren, callee = tokens[opener - 1], tokens[opener - 2]
                dotted = opener >= 3 and tokens[opener - 3].kind == 'punct' and (
                    tokens[opener - 3].text in {'.', '?.'})
                placed = (
                    paren.kind == 'punct' and paren.text == '('
                    and callee.kind == 'name' and callee.text == 'parallel'
                    and not dotted
                    and after is not None and after.kind == 'punct'
                    and after.text == ')')
            elif opener is not None and tokens[opener].text == '(' and opener >= 2:
                method, dot = tokens[opener - 1], tokens[opener - 2]
                placed = (
                    method.kind == 'name' and method.text in CONTINUATION_METHODS
                    and dot.kind == 'punct' and dot.text in {'.', '?.'})
            if len(arrows) > 1 or not placed:
                return f'a function at line {tokens[arrows[-1]].line}'
        if opener is None:
            return None
        token = tokens[opener]
        prev = tokens[opener - 1] if opener else None
        after_paren = prev is not None and prev.kind == 'punct' and prev.text == ')'
        closer = partner[opener]
        arrow = tokens[closer + 1] if closer + 1 < len(tokens) else None
        if token.text == '(' and arrow is not None and arrow.text == '=>':
            return f'a function at line {arrow.line}'
        if token.text == '(' and prev is not None:
            if prev.kind == 'punct' and prev.text in {')', ']', '}'}:
                return f'a call at line {token.line} the gate cannot count'
            if prev.kind not in {'punct', 'name'}:
                return f'a call at line {token.line} the gate cannot count'
            before = tokens[opener - 2] if opener >= 2 else None
            is_method = before is not None and before.kind == 'punct' and (
                before.text in {'.', '?.'})
            if prev.kind == 'name' and is_method:
                owner = tokens[opener - 3] if opener >= 3 else None
                is_promise = owner is not None and owner.kind == 'name' and (
                    owner.text == 'Promise')
                is_known = prev.text in CONTINUATION_METHODS or (
                    prev.text in PROMISE_COMBINATORS and is_promise)
                if not is_known:
                    return f'a call to .{prev.text}() at line {token.line}'
            elif prev.kind == 'name' and prev.text in LOOP_KEYWORDS:
                return f'a {prev.text} loop at line {token.line}'
            elif prev.kind == 'name' and prev.text not in PASSING_HEADS:
                return f'a call to {prev.text}() at line {token.line}'
        elif token.text == '{' and after_paren:
            head_index = partner[opener - 1] - 1
            head = tokens[head_index] if head_index >= 0 else None
            is_head_name = head is not None and head.kind == 'name'
            if is_head_name and head.text in LOOP_KEYWORDS:
                return f'a {head.text} loop at line {head.line}'
            if is_head_name and head.text not in CONDITION_KEYWORDS:
                return f'a function body at line {token.line}'
        position = opener
    return None


@dataclasses.dataclass
class Stage:
    """One agent() call in a Workflow script.

    Attributes
    ----------
    line : int
        The line of the call.
    tier : str
        The literal agentType, or '' when the options name none.
    label : str or None
        The literal label option, for the log.
    prefix : str or None
        The cooked static text the prompt opens with, or None when the
        prompt does not open with a literal.
    multiplied : str or None
        Why the call may run more than once, or None when it runs at
        most once.
    """

    line: int
    tier: str
    label: str | None
    prefix: str | None
    multiplied: str | None


def script_stages(
    tokens: list[Token],
    partner: dict[int, int],
    enclosing: list[tuple[int, ...]]) -> list[Stage]:
    """Return every agent() stage of a script, in source order.

    Parameters
    ----------
    tokens : list[Token]
        The tokenized script.
    partner : dict[int, int]
        Bracket pairs from match_brackets.
    enclosing : list[tuple[int, ...]]
        Enclosing openers from match_brackets.

    Returns
    -------
    list[Stage]
        One entry per call of the agent function.

    Notes
    -----
    - A bare name in RESERVED_NAMES, the name constructor anywhere, and
      the name agent used other than as an immediate call or an object
      key raise ScriptError: each is a way to reach the agent function,
      or another script, that the gate would not see. A declaration of
      agent or parallel, a bare then, and a catch or finally object
      member raise too: the walk trusts those names to run a thunk or an
      argument once.
    - An agent() call takes a prompt and one object literal. The literal
      names agentType as a string literal, and carries no model or
      effort key, since the tier pins both. A meta phase entry carrying
      either key is refused for the same reason.
    """
    stages: list[Stage] = []
    for index, token in enumerate(tokens):
        if token.kind != 'name':
            continue
        prev = tokens[index - 1] if index else None
        is_property = prev is not None and prev.kind == 'punct' and (
            prev.text in {'.', '?.'})
        is_reserved = token.text in RESERVED_NAMES and not is_property
        if token.text == 'constructor' or is_reserved:
            raise ScriptError(
                f'{token.text} at line {token.line} is not allowed in a Workflow '
                'script')
        is_meta = (
            token.text == 'meta' and prev is not None and prev.kind == 'name'
            and prev.text in {'const', 'let', 'var'} and index + 2 < len(tokens)
            and tokens[index + 1].kind == 'punct' and tokens[index + 1].text == '='
            and tokens[index + 2].kind == 'punct' and tokens[index + 2].text == '{')
        if is_meta:
            meta_entries = object_entries(
                tokens, partner, enclosing, index + 2, 'meta')
            phases = meta_entries.get('phases') or []
            is_array = bool(phases) and tokens[phases[0]].text == '[' and (
                partner.get(phases[0]) == phases[-1])
            phase_openers: list[int] = []
            if is_array:
                inside_phases = (*enclosing[phases[0]], phases[0])
                phase_openers = [
                    j for j in range(phases[0] + 1, phases[-1])
                    if tokens[j].text == '{' and enclosing[j] == inside_phases]
            for j in phase_openers:
                entry = object_entries(tokens, partner, enclosing, j, 'a meta phase')
                for key in STAGE_PINNED_OPTIONS:
                    if key in entry:
                        raise ScriptError(
                            f'a meta phase at line {tokens[j].line} sets {key}; the '
                            'tier pins the model and effort')
        follower = tokens[index + 1] if index + 1 < len(tokens) else None
        is_call = follower is not None and follower.kind == 'punct' and (
            follower.text == '(')
        is_key = follower is not None and follower.kind == 'punct' and (
            follower.text == ':')
        # Notes:
        # - parallel is trusted to run each thunk once, and .then(),
        #   .catch(), and .finally() to run their argument once. A
        #   script that binds parallel, defines then, or gives an object
        #   a catch or finally member could run them any number of
        #   times.
        declared = prev is not None and prev.kind == 'name' and prev.text in DECLARATORS
        if token.text in {'agent', *TRUSTED_CALLS} and declared:
            raise ScriptError(f'{token.text} at line {token.line} may not be declared')
        if token.text in TRUSTED_CALLS and not is_property and not is_call:
            raise ScriptError(f'{token.text} at line {token.line} may only be called')
        if token.text == 'then' and not is_property:
            raise ScriptError(
                f'then at line {token.line} may only be a property call')
        is_member = is_key or (
            prev is not None and prev.kind == 'punct' and prev.text in {'{', ','})
        if token.text in {'catch', 'finally'} and is_member and not is_property:
            raise ScriptError(
                f'{token.text} at line {token.line} may not be an object member')
        if token.text != 'agent' or is_property or is_key:
            continue
        if not is_call:
            raise ScriptError(
                f'agent at line {token.line} is used other than as a call')
        opener = index + 1
        inside = (*enclosing[opener], opener)
        args: list[list[int]] = [[]]
        for j in range(opener + 1, partner[opener]):
            is_comma = tokens[j].kind == 'punct' and tokens[j].text == ','
            if enclosing[j] == inside and is_comma:
                args.append([])
            else:
                args[-1].append(j)
        if not args[-1]:
            args.pop()
        if not args or len(args) > 2 or not all(args):
            raise ScriptError(
                f'agent() at line {token.line} takes a prompt and an options object')
        first = tokens[args[0][0]]
        prefix = first.value if first.kind in {'string', 'template'} else None
        tier = ''
        label = None
        if len(args) == 2:
            options = args[1]
            if tokens[options[0]].text != '{' or partner.get(options[0]) != options[-1]:
                raise ScriptError(
                    f'the options of agent() at line {token.line} must be one object '
                    'literal')
            entries = object_entries(
                tokens, partner, enclosing, options[0], 'the options of agent()')
            for key in STAGE_PINNED_OPTIONS:
                if key in entries:
                    raise ScriptError(
                        f'agent() at line {token.line} sets {key}; the tier pins the '
                        'model and effort, so drop it')
            for key in ('agentType', 'label'):
                value = entries.get(key)
                if value is None:
                    continue
                literal = tokens[value[0]]
                is_literal = len(value) == 1 and (
                    literal.kind == 'string'
                    or (literal.kind == 'template' and literal.static))
                if not is_literal and key == 'agentType':
                    raise ScriptError(
                        f'agentType at line {literal.line} must be a string literal '
                        f'naming a tier: {TIERS_TEXT}')
                if is_literal and key == 'agentType':
                    tier = literal.value or ''
                elif is_literal:
                    label = literal.value
        multiplied = multiplier(tokens, partner, enclosing, index)
        stages.append(Stage(token.line, tier, label, prefix, multiplied))
    return stages


def gate_workflow(hook_input: dict[str, Any]) -> dict[str, Any] | None:
    """Decide one Workflow call from its script text.

    Parameters
    ----------
    hook_input : dict[str, Any]
        The PreToolUse payload for a Workflow call.

    Returns
    -------
    dict[str, Any] or None
        A deny decision, or None to let the call continue.

    Notes
    -----
    - The script is the inline script, else the scriptPath file. A saved
      workflow name is denied: the runtime resolves it by the meta.name
      inside each .claude/workflows/*.js file, so the gate cannot tell
      which script runs. A script that cannot be read is denied.
    - Every agent() stage is checked in source order with the rules of
      gate_agent: tier named, header syntax, header present on a capped
      stage, round, opus-cap, and derive values, derive on a deriving
      tier alone, opus-cap presence on a capped review, verify, or swarm
      stage. Four rules are Workflow's own: agentType names one of the
      eight prefixed tiers and nothing else, so Explore, Plan, and fork
      deny here and pass on Agent; it compares as written, prefix and
      case included, where gate_agent lowercases; a capped stage must run
      at most once; and it must open its prompt with a literal whose text
      before the first substitution is the header. The first failing
      stage denies the call.
    - The marked capped stages then reserve their slots in one batch on
      the same cycle counters Agent launches use; a refusal denies the
      call and moves no counter. A cheap stage never takes a slot, in any
      quantity.
    - One log line per stage; a script with no stage logs one line with
      scope no-stages.
    - An allowed script with a stage on either deriving tier returns a
      systemMessage envelope, one line per stage, as an Agent launch
      does.
    """
    tool_input = hook_input.get('tool_input') or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    tiers = TIERS_TEXT
    event: dict[str, Any] = {
        'tool': 'Workflow',
        'session': hook_input.get('session_id'),
        'model': None,
        'agent_type': None,
        'label': None,
        'source': 'script',
        }
    script = tool_input.get('script')
    if not script and tool_input.get('name'):
        # The runtime resolves a saved name by the meta.name inside each
        # .claude/workflows/*.js file, not by file name, and may hold
        # built-in workflows too; the gate cannot know which script will
        # run, so it reads none.
        return deny(
            'review-gate: the gate cannot tell which script the saved workflow '
            f'{tool_input["name"]} resolves to; pass the script inline or as a '
            'readable scriptPath.',
            {**event, 'source': str(tool_input['name'])})
    if not script:
        cwd = pathlib.Path(str(hook_input.get('cwd') or '.'))
        named = str(tool_input.get('scriptPath') or '')
        path = pathlib.Path(named).expanduser() if named else None
        if path is not None and not path.is_absolute():
            path = cwd / path
        try:
            script = path.read_text(encoding='utf-8') if path is not None else ''
        except (OSError, ValueError):
            script = ''
        if not script:
            return deny(
                'review-gate: the gate reads a Workflow from its script and cannot '
                f'read {named or "an empty call"}; pass the script inline or as a '
                'readable scriptPath.',
                {**event, 'source': named or None})
        event['source'] = str(path)

    try:
        tokens = tokenize(str(script))
        partner, enclosing = match_brackets(tokens)
        stages = script_stages(tokens, partner, enclosing)
    except ScriptError as exc:
        return deny(f'review-gate: {exc}.', event)
    if not stages:
        allow({**event, 'scope': 'no-stages'})
        return None

    site_events: list[dict[str, Any]] = []
    requests: list[tuple[int, str, str | None, bool]] = []
    for stage in stages:
        site = {
            **event,
            'agent_type': stage.tier or None,
            'label': stage.label,
            'line': stage.line,
            }
        where = f'Workflow stage at line {stage.line}'
        scoped, tier = split_type(stage.tier)
        if not scoped and tier in INHERITING_TYPES:
            return deny(
                f'review-gate: {where}: name the tier: agentType {tiers}. '
                'general-purpose and an omitted agentType inherit the main-loop '
                'model and effort.',
                site)
        if not scoped and tier in TIERS:
            return deny(
                f"review-gate: {where}: the tiers are the {PLUGIN_NAME} plugin's "
                f'agents; name agentType {TIER_PREFIX}{tier}, not {stage.tier}.',
                site)
        if not scoped or tier not in TIERS:
            return deny(
                f'review-gate: {where}: agentType {stage.tier} is not a tier; '
                f'use {tiers}.',
                site)
        is_capped = tier in CAPPED_TIERS
        if is_capped and stage.multiplied:
            return deny(
                f'review-gate: {where}: a capped stage inside {stage.multiplied} '
                'may run more than once. Write each capped stage as one agent() '
                'call at the top level, a thunk in parallel([...]), or a .then(), '
                '.catch(), or .finally() continuation; fan-out runs on '
                f'{TIER_PREFIX}sonnet-high or {TIER_PREFIX}haiku.',
                site)
        if is_capped and stage.prefix is None:
            return deny(
                f'review-gate: {where}: a capped stage opens its prompt with a '
                'string or template literal so the gate can read its review-gate '
                'header; a variable, a concatenation, or a helper call hides it. '
                'Write the header as the first text of the literal, then ${...} '
                'for the rest.',
                site)
        if is_capped and stage.prefix == '':
            return deny(
                f'review-gate: {where}: a capped stage opens its prompt literal '
                'with the review-gate header itself; a leading ${...} hides it, '
                'since only the text before the first substitution is read. Write '
                'the header text first, then the substitution.',
                site)
        header, header_error = None, None
        if stage.prefix is not None:
            header, header_error = parse_header(stage.prefix)
        if header_error:
            return deny(f'review-gate: {where}: {header_error}.', site)
        if header is None:
            if is_capped:
                return deny(
                    f'review-gate: {where}: a capped stage opens its prompt with '
                    'a <review-gate> header; capped work outside a review declares '
                    'round: swarm and an opus-cap.',
                    site)
            site_events.append({**site, 'scope': 'not-review-marked'})
            continue
        round_name = header.get('round')
        if round_name not in ROUNDS:
            return deny(
                f'review-gate: {where}: round must be review, verify, synthesize, '
                'or swarm.',
                {**site, 'round': round_name})
        site['round'] = round_name
        opus_cap = header.get('opus-cap')
        if opus_cap is not None and opus_cap not in CAPS:
            return deny(
                f'review-gate: {where}: invalid opus-cap. Use 3, 6, or 9; the cap is '
                'one of three values, not a free number.',
                {**site, 'opus_cap': opus_cap})
        derive = header.get('derive')
        if derive is not None and derive not in DERIVE_KINDS:
            return deny(
                f'review-gate: {where}: invalid derive: {derive}. The kinds are '
                f'{KINDS_TEXT}; a kind names what the agent must derive, and '
                'importance, breadth, and subject matter are not kinds. Where none '
                f'fits the brief, use {HIGH_TIERS_TEXT}.',
                {**site, 'derive': derive})
        if derive is not None and tier not in SEAT_TIERS:
            return deny(
                f'review-gate: {where}: derive belongs on a deriving tier; '
                f'{stage.tier} declared derive: {derive}. Drop the field, or run '
                f'the deriving brief on {SEAT_TIERS_TEXT}.',
                {**site, 'derive': derive})
        if not is_capped:
            site_events.append({**site, 'scope': 'uncapped'})
            continue
        takes_seat = tier in SEAT_TIERS
        if takes_seat and derive is None:
            return deny(
                f'review-gate: {where}: {stage.tier} needs derive: <kind> in the '
                f'header, one of {KINDS_TEXT}. Where an oracle outside the agent '
                'checks the result - a spec, a schema, a test run, the callers - '
                f'use {HIGH_TIERS_TEXT}.',
                {**site, 'opus_cap': opus_cap})
        if round_name in OPUS_CAP_ROUNDS and opus_cap is None:
            return deny(
                f'review-gate: {where}: capped review, verify, and swarm stages '
                'require opus-cap: 3, 6, or 9 in the leading header.',
                site)
        site['opus_cap'] = opus_cap
        if takes_seat:
            site['derive'] = derive
            site['tier'] = tier
        requests.append((len(site_events), round_name, opus_cap, takes_seat))
        site_events.append(site)

    seat_lines: list[str] = []
    if requests:
        turn = turn_key(hook_input)
        if turn is None:
            for position, _, _, takes_seat in requests:
                site = site_events[position]
                site['scope'] = 'no-turn'
                if takes_seat:
                    seat_lines.append(seat_line(
                        f'{site["tier"]} uncounted, no cycle key',
                        site['derive'],
                        stage_label(site)))
        else:
            session_id = str(hook_input.get('session_id') or 'nosession')
            slots = reserve_slots(
                session_id, turn, [request[1:] for request in requests])
            for request, slot in zip(requests, slots):
                position, round_name, _, takes_seat = request
                site = site_events[position]
                site['turn'] = turn
                if slot.refusal != 'seat':
                    site.update({'round_n': slot.number, 'round_cap': slot.cap})
                if slot.seat is not None:
                    site.update({'seat_n': slot.seat, 'seat_cap': slot.seats})
                if slot.refusal is None:
                    if takes_seat:
                        seat_lines.append(seat_line(
                            f'{site["tier"]} seat {slot.seat}/{slot.seats}',
                            site['derive'],
                            stage_label(site)))
                    continue
                site['refusal'] = slot.refusal
                if slot.refusal == 'mismatch':
                    site['fixed'] = slot.fixed
                return deny(
                    f'review-gate: Workflow stage at line {site["line"]}: '
                    f'{refusal_reason(slot, round_name)}',
                    site)
    for site in site_events:
        allow(site)
    if seat_lines:
        return {'systemMessage': '\n'.join(seat_lines)}
    return None


def main() -> None:
    """Read the hook payload on stdin and print a deny decision when needed.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (TypeError, ValueError):
        return
    if not isinstance(hook_input, dict):
        return
    tool = hook_input.get('tool_name')
    if tool not in {'Agent', 'Workflow'}:
        return
    try:
        output = (gate_agent if tool == 'Agent' else gate_workflow)(hook_input)
    except Exception as exc:  # noqa: BLE001
        log_event({
            'tool': tool,
            'session': hook_input.get('session_id'),
            'decision': 'error',
            'reason': repr(exc)[:300],
            })
        return
    if output is not None:
        json.dump(output, sys.stdout)


if __name__ == '__main__':
    main()
