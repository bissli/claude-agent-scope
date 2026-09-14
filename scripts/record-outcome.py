"""SubagentStop outcome recorder for the review-gate plugin.

Records size and digest of the last assistant message to outcome.jsonl
when a subagent stops. Never stores the message text.

Notes
-----
- outcome.jsonl lives in STATE_HOME beside gate.jsonl; they are separate
  files so existing gate.jsonl readers require no change.
- An Agent launch joins to its gate.jsonl row through tool_use_id: the
  gate records the PreToolUse tool_use_id, meta.json carries it as
  toolUseId, and SubagentStop names that file through agent_id. The
  link is one to one.
- A Workflow stage carries no toolUseId, so it does not join. Most
  stages carry no description either, and agentType reads
  workflow-subagent or general-purpose rather than the gate's tier
  name, so such a row shares only session with the gate log.
- Whether SubagentStop fires for a Workflow stage is unconfirmed.
- A write failure is swallowed; logging must never block a session.
"""

import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any

STATE_HOME = pathlib.Path(
    os.environ.get('REVIEW_GATE_HOME', '~/.claude/cache/review-gate')).expanduser()
OUTCOME_PATH = STATE_HOME / 'outcome.jsonl'


def session_from_transcript(transcript: pathlib.Path) -> str | None:
    """Extract the parent session id from a subagent transcript path.

    Parameters
    ----------
    transcript : pathlib.Path
        Path to the subagent transcript .jsonl file.

    Returns
    -------
    str or None
        The session directory name (UUID) that sits immediately above the
        ``subagents`` component of the path, or None when no ``subagents``
        component is found.

    Notes
    -----
    - Agent path shape: PROJECT/SESSION/subagents/agent-ID.jsonl
    - Workflow stage shape:
      PROJECT/SESSION/subagents/workflows/WF/agent-ID.jsonl
    - Both yield the SESSION component: the directory above
      ``subagents``.
    """
    parts = transcript.parts
    for i, part in enumerate(parts):
        if part == 'subagents':
            return parts[i - 1] if i > 0 else None
    return None


def read_meta(transcript: pathlib.Path) -> dict[str, Any]:
    """Read the meta.json file alongside a subagent transcript.

    Parameters
    ----------
    transcript : pathlib.Path
        Path to the subagent transcript .jsonl file. The meta.json sits
        in the same directory with the same stem and a .meta.json suffix.

    Returns
    -------
    dict[str, Any]
        Parsed meta.json, or an empty dict when the file is absent,
        unreadable, or not valid JSON.
    """
    meta_path = transcript.parent / (transcript.stem + '.meta.json')
    try:
        return json.loads(meta_path.read_text('utf-8'))
    except (OSError, ValueError):
        return {}


def msg_digest(text: str) -> str:
    """Return the first 16 hex characters of the SHA-256 of text.

    Parameters
    ----------
    text : str
        The message text to hash.

    Returns
    -------
    str
        A 16-character lowercase hex string (64 bits of SHA-256), stable
        for identical text and distinct for distinct text with high
        probability.
    """
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:16]


def record_stop(hook_input: dict[str, Any]) -> None:
    """Append one outcome row to outcome.jsonl from a SubagentStop payload.

    Parameters
    ----------
    hook_input : dict[str, Any]
        The SubagentStop payload as Claude Code sends it on stdin.
        Expected fields: agent_id, agent_transcript_path, agent_type,
        last_assistant_message (optional).

    Notes
    -----
    - The message text is never stored; only its byte size, character
      count, and digest are recorded.
    - A write failure or any unexpected error is swallowed so the hook
      never blocks the session.
    """
    try:
        agent_id = hook_input.get('agent_id')
        agent_type = hook_input.get('agent_type')
        raw_path = hook_input.get('agent_transcript_path') or ''
        transcript = pathlib.Path(raw_path) if raw_path else None

        session = None
        tool_use_id = None
        label = None

        if transcript:
            session = session_from_transcript(transcript)
            meta = read_meta(transcript)
            tool_use_id = meta.get('toolUseId')
            label = meta.get('description')
            if not agent_type:
                agent_type = meta.get('agentType')

        msg = hook_input.get('last_assistant_message') or ''
        row = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'record_type': 'outcome',
            'agent_id': agent_id,
            'session': session,
            'tool_use_id': tool_use_id,
            'agent_type': agent_type,
            'label': label,
            'msg_size_bytes': len(msg.encode('utf-8')),
            'msg_size_chars': len(msg),
            'msg_digest': msg_digest(msg),
        }
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        with OUTCOME_PATH.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=True, default=str) + '\n')
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    """Read a SubagentStop payload from stdin and record the outcome.

    Notes
    -----
    - Ignores stdin that is not valid JSON, not a dict, or not a
      SubagentStop event.
    - Writes nothing to stdout; a stray print would disrupt Claude
      Code's JSON stream parsing.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (TypeError, ValueError):
        return
    if not isinstance(hook_input, dict):
        return
    if hook_input.get('hook_event_name') != 'SubagentStop':
        return
    record_stop(hook_input)


if __name__ == '__main__':
    main()
