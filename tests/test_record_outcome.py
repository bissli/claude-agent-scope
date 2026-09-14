"""Tests for the SubagentStop outcome recorder.

Payloads are built from the binary schema:
    hook_event_name:R("SubagentStop"), stop_hook_active, agent_id,
    agent_transcript_path, agent_type,
    last_assistant_message (optional), background_tasks (optional)

The filesystem layout for Agent and Workflow stages is reproduced in
tmp_path, including meta.json, so tests exercise real path parsing rather
than invented inputs.
"""

import io
import json
import pathlib

import pytest

AGENT_ID = 'a57143e37ad13b024'
SESSION_ID = 'fake-session-uuid'


# --- payload and fixture helpers ---


def make_transcript(tmp_path: pathlib.Path, *, workflow: bool = False) -> pathlib.Path:
    """Build a fake subagent transcript tree in tmp_path.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Root for the temporary directory tree.
    workflow : bool, default False
        When True, use the Workflow stage path shape
        (SESSION/subagents/workflows/WF_ID/agent-ID.jsonl).
        When False, use the Agent path shape
        (SESSION/subagents/agent-ID.jsonl).

    Returns
    -------
    pathlib.Path
        Path to the (empty) transcript .jsonl file.
    """
    if workflow:
        subagents = tmp_path / SESSION_ID / 'subagents' / 'workflows' / 'wf_abc'
    else:
        subagents = tmp_path / SESSION_ID / 'subagents'
    subagents.mkdir(parents=True)
    transcript = subagents / f'agent-{AGENT_ID}.jsonl'
    transcript.touch()
    return transcript


def write_meta(
    transcript: pathlib.Path,
    *,
    tool_use_id: str | None = None,
    description: str | None = None,
    agent_type: str = 'agent-scope:opus-high',
) -> None:
    """Write meta.json alongside the transcript file.

    Parameters
    ----------
    transcript : pathlib.Path
        Path to the transcript .jsonl file.
    tool_use_id : str or None
        Value of toolUseId in meta.json; absent when None (Workflow stage).
    description : str or None
        Value of description in meta.json; absent when None.
    agent_type : str
        Value of agentType in meta.json.
    """
    meta: dict = {'agentType': agent_type, 'spawnDepth': 1}
    if tool_use_id is not None:
        meta['toolUseId'] = tool_use_id
    if description is not None:
        meta['description'] = description
    meta_path = transcript.parent / (transcript.stem + '.meta.json')
    meta_path.write_text(json.dumps(meta), encoding='utf-8')


def subagent_stop_payload(
    transcript: pathlib.Path,
    *,
    agent_id: str = AGENT_ID,
    agent_type: str = 'agent-scope:opus-high',
    last_assistant_message: str = 'Finding: missing null check on line 42.',
    stop_hook_active: bool = False,
) -> dict:
    """Build a SubagentStop payload from the binary schema fields.

    Parameters
    ----------
    transcript : pathlib.Path
        Path to the subagent transcript .jsonl file.
    agent_id : str
        The subagent id (matches the agentId from PostToolUse tool_response).
    agent_type : str
        The model tier the subagent ran on.
    last_assistant_message : str
        Text content of the last assistant message before stopping.
    stop_hook_active : bool
        Whether a stop hook is active.

    Returns
    -------
    dict
        Payload shaped to the binary SubagentStop schema.
    """
    return {
        'hook_event_name': 'SubagentStop',
        'stop_hook_active': stop_hook_active,
        'agent_id': agent_id,
        'agent_transcript_path': str(transcript),
        'agent_type': agent_type,
        'last_assistant_message': last_assistant_message,
    }


def last_row(outcome) -> dict:
    """Return the last row of outcome.jsonl as a dict.
    """
    return json.loads(outcome.OUTCOME_PATH.read_text().splitlines()[-1])


def all_rows(outcome) -> list[dict]:
    """Return every row of outcome.jsonl as a list of dicts.
    """
    text = outcome.OUTCOME_PATH.read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --- outcome row fields ---


def test_outcome_row_written_on_stop(outcome, tmp_path):
    """Verify a SubagentStop event produces one outcome row.

    Mutation: returning early before writing, so no row is ever recorded.
    Oracle: outcome.jsonl contains exactly one row after record_stop runs.
    """
    transcript = make_transcript(tmp_path)
    write_meta(transcript, tool_use_id='toolu_abc', description='review:bugs')
    outcome.record_stop(subagent_stop_payload(transcript))
    rows = all_rows(outcome)
    assert len(rows) == 1
    assert rows[0]['record_type'] == 'outcome'


def test_agent_id_recorded(outcome, tmp_path):
    """Verify agent_id from the payload is stored in the outcome row.

    Mutation: storing hook_event_name instead of agent_id, or storing None.
    Oracle: row['agent_id'] matches the agent_id field in the payload.
    """
    transcript = make_transcript(tmp_path)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(transcript, agent_id='a57143e37ad13b024'))
    row = last_row(outcome)
    assert row['agent_id'] == 'a57143e37ad13b024'


def test_session_extracted_from_agent_transcript_path(outcome, tmp_path):
    """Verify session is derived from the Agent transcript path.

    Mutation: taking path.parent.name (the 'subagents' dir) instead of the
        directory above it, recording 'subagents' as the session.
    Oracle: session equals SESSION_ID, which is the directory above 'subagents'
        in the Agent path shape SESSION/subagents/agent-ID.jsonl.
    """
    transcript = make_transcript(tmp_path, workflow=False)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(transcript))
    row = last_row(outcome)
    assert row['session'] == SESSION_ID


def test_session_extracted_from_workflow_transcript_path(outcome, tmp_path):
    """Verify session is derived from a Workflow stage transcript path.

    Mutation: using the wrong ancestor depth for the Workflow path shape
        (SESSION/subagents/workflows/WF/agent-ID.jsonl), recording
        the workflow id or 'workflows' instead of the session uuid.
    Oracle: session equals SESSION_ID regardless of path depth.
    """
    transcript = make_transcript(tmp_path, workflow=True)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(transcript))
    row = last_row(outcome)
    assert row['session'] == SESSION_ID


def test_tool_use_id_from_meta_for_agent_launch(outcome, tmp_path):
    """Verify tool_use_id is read from meta.json for Agent launches.

    Mutation: omitting the meta.json read, leaving tool_use_id always None.
    Oracle: row['tool_use_id'] matches the toolUseId written in meta.json.
    """
    transcript = make_transcript(tmp_path)
    write_meta(transcript, tool_use_id='toolu_01MRTJ3jv6ZExtBhzxVKMZ2N')
    outcome.record_stop(subagent_stop_payload(transcript))
    row = last_row(outcome)
    assert row['tool_use_id'] == 'toolu_01MRTJ3jv6ZExtBhzxVKMZ2N'


def test_tool_use_id_none_for_workflow_stage(outcome, tmp_path):
    """Verify tool_use_id is None for Workflow stages whose meta.json lacks toolUseId.

    Mutation: storing a default string 'unknown' when toolUseId is absent,
        making Workflow rows look like they have a bridge key they do not.
    Oracle: row['tool_use_id'] is None when meta.json has no toolUseId.
    """
    transcript = make_transcript(tmp_path, workflow=True)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(transcript))
    row = last_row(outcome)
    assert row['tool_use_id'] is None


def test_label_from_meta_description(outcome, tmp_path):
    """Verify label is read from meta.json description field.

    Mutation: reading label from the payload's agent_type instead of
        meta.json description, recording the model tier as the label.
    Oracle: row['label'] matches the description written in meta.json.
    """
    transcript = make_transcript(tmp_path)
    write_meta(transcript, description='review:correctness')
    outcome.record_stop(subagent_stop_payload(transcript))
    row = last_row(outcome)
    assert row['label'] == 'review:correctness'


def test_label_none_for_workflow_stage(outcome, tmp_path):
    """Verify label is None for Workflow stages whose meta.json lacks description.

    Mutation: defaulting label to the agentType string when description is absent.
    Oracle: row['label'] is None when meta.json has no description field.
    """
    transcript = make_transcript(tmp_path, workflow=True)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(transcript))
    row = last_row(outcome)
    assert row['label'] is None


# --- message size and digest ---


def test_msg_size_bytes_and_chars_match_ascii(outcome, tmp_path):
    """Verify msg_size_bytes and msg_size_chars are equal for ASCII text.

    Mutation: computing bytes from char count or vice versa; for ASCII
        both are equal so the swap is invisible without a non-ASCII test.
    Oracle: 'hello' is 5 bytes and 5 chars in UTF-8.
    """
    transcript = make_transcript(tmp_path)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(
        transcript, last_assistant_message='hello'))
    row = last_row(outcome)
    assert row['msg_size_bytes'] == 5
    assert row['msg_size_chars'] == 5


def test_msg_size_bytes_differs_from_chars_for_non_ascii(outcome, tmp_path):
    """Verify msg_size_bytes counts UTF-8 bytes, not characters.

    Mutation: using len(text) for msg_size_bytes, giving the character count
        instead of the byte count for text with multi-byte codepoints.
    Oracle: the euro sign U+20AC encodes to 3 bytes in UTF-8, so
        msg_size_bytes must be 3 while msg_size_chars is 1.
    """
    transcript = make_transcript(tmp_path)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(transcript, last_assistant_message='€'))
    row = last_row(outcome)
    assert row['msg_size_bytes'] == 3
    assert row['msg_size_chars'] == 1


def test_message_text_not_stored(outcome, tmp_path):
    """Verify the last_assistant_message text is absent from outcome.jsonl.

    Mutation: writing the full last_assistant_message into the row, exposing
        session content in the log file.
    Oracle: the known message string does not appear in the raw file bytes.
    """
    secret = 'PRIVATE-SESSION-CONTENT-XYZ'
    transcript = make_transcript(tmp_path)
    write_meta(transcript)
    outcome.record_stop(subagent_stop_payload(
        transcript, last_assistant_message=secret))
    raw = outcome.OUTCOME_PATH.read_bytes()
    assert secret.encode() not in raw


def test_digest_is_truncated_sha256(outcome):
    """Verify msg_digest is the first 16 hex chars of the UTF-8 SHA-256.

    Mutation: swapping sha256 for md5, widening the truncation, or
        digesting str(text) rather than its UTF-8 bytes.
    Oracle: hand-computed sha256(b'hello world').hexdigest()[:16].
    """
    assert outcome.msg_digest('hello world') == 'b94d27b9934d3e08'
    assert outcome.msg_digest('') == 'e3b0c44298fc1c14'


def test_digest_differs_for_distinct_text(outcome):
    """Verify msg_digest produces distinct values for distinct inputs.

    Mutation: returning a constant digest regardless of input, making
        the digest field meaningless as a change detector.
    Oracle: msg_digest('text-one') != msg_digest('text-two').
    """
    assert outcome.msg_digest('text-one') != outcome.msg_digest('text-two')


# --- resilience ---


def test_hook_produces_no_stdout(outcome, tmp_path, capsys, monkeypatch):
    """Verify the hook entry point writes nothing to stdout.

    Mutation: a print() or a decision envelope emitted anywhere under
        main(), which Claude Code parses as a SubagentStop decision and
        would act on.
    Oracle: capsys.readouterr().out is empty after main() consumes a
        well-formed payload from stdin.
    """
    transcript = make_transcript(tmp_path)
    write_meta(transcript)
    payload = json.dumps(subagent_stop_payload(transcript))
    monkeypatch.setattr('sys.stdin', io.StringIO(payload))
    outcome.main()
    captured = capsys.readouterr()
    assert captured.out == ''


def test_hook_swallows_malformed_payload(outcome):
    """Verify record_stop does not raise on malformed or None input.

    Mutation: removing the outer try/except, letting a TypeError or
        AttributeError crash the hook process on bad input.
    Oracle: no exception raised for None, an empty dict, or a dict
        with a non-path agent_transcript_path.
    """
    try:
        outcome.record_stop(None)
        outcome.record_stop({})
        outcome.record_stop({'agent_transcript_path': ''})
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f'record_stop raised: {exc!r}')


def test_hook_ignores_non_subagent_stop_event(outcome, tmp_path, monkeypatch):
    """Verify main() exits without writing when hook_event_name is not SubagentStop.

    Mutation: removing the hook_event_name guard, writing a row for every
        hook event the script receives (PostToolUse, SubagentStart, etc.).
    Oracle: outcome.jsonl is absent after main() processes a PostToolUse payload.
    """
    payload = json.dumps({
        'hook_event_name': 'PostToolUse',
        'tool_name': 'Agent',
        'session_id': 'session-1',
        'tool_response': 'ok',
    })
    monkeypatch.setattr('sys.stdin', io.StringIO(payload))
    outcome.main()
    assert not outcome.OUTCOME_PATH.exists()


def test_main_handles_invalid_json(outcome, monkeypatch):
    """Verify main() does not raise on stdin that is not valid JSON.

    Mutation: removing the try/except around json.load, crashing the hook
        whenever Claude Code sends a non-JSON payload on stdin.
    Oracle: no exception raised; outcome.jsonl is absent.
    """
    monkeypatch.setattr('sys.stdin', io.StringIO('NOT JSON'))
    try:
        outcome.main()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f'main() raised on bad JSON: {exc!r}')
    assert not outcome.OUTCOME_PATH.exists()


# --- session_from_transcript ---


def test_session_from_transcript_returns_none_when_no_subagents(outcome, tmp_path):
    """Verify session_from_transcript returns None for a path lacking 'subagents'.

    Mutation: returning parts[-2] unconditionally, giving a wrong result
        for paths that have no 'subagents' component.
    Oracle: None returned for a path without 'subagents' in its components.
    """
    path = tmp_path / 'project' / 'session' / 'other' / 'agent-abc.jsonl'
    result = outcome.session_from_transcript(path)
    assert result is None


# --- read_meta ---


def test_read_meta_returns_empty_when_file_absent(outcome, tmp_path):
    """Verify read_meta returns an empty dict when meta.json does not exist.

    Mutation: raising OSError instead of catching it, crashing record_stop
        for subagents whose meta.json was not written.
    Oracle: empty dict returned for a transcript with no sibling meta.json.
    """
    transcript = tmp_path / 'agent-abc.jsonl'
    transcript.touch()
    result = outcome.read_meta(transcript)
    assert result == {}
