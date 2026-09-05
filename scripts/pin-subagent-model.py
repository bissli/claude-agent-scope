#!/usr/bin/env python3
"""PreToolUse hook that keeps Opus subagents off the main-loop Opus version.

Claude Code resolves a subagent's model as per-invocation ``model``
parameter -> definition ``model`` frontmatter -> ``CLAUDE_CODE_SUBAGENT_MODEL``
-> main-loop model. The per-invocation step collapses any *family alias*
naming the main loop's own family straight onto the main-loop model, so a
main loop pinned to one Opus version hands that same version to every
subagent that merely asks for ``opus``.

Notes
-----
- The ``Agent`` tool's ``model`` parameter is enum-validated
  (``sonnet``/``opus``/``haiku``/``fable``), so a hook cannot inject a full
  model id here; doing so fails schema validation. Only definition frontmatter
  accepts a full model id, and a full id is what escapes the collapse.
- Therefore this hook *removes* a bare Opus alias instead of rewriting it,
  letting resolution fall through to the frontmatter pin in the plugin's
  ``agents/`` directory. That pin is the single place the Opus version is
  named; nothing here encodes a version.
- A full ``claude-opus-*`` id passes through untouched: it never collapses, so
  an explicit version request is honored as given.
- Non-Opus aliases pass through untouched so an explicit ``fable``, ``haiku``,
  or ``sonnet`` request is still granted.
- ``Explore`` is pinned to ``haiku`` when the caller names no model, keeping
  grep-fanout work off the expensive main-loop model.
"""

import json
import sys
from typing import Any

MODEL_DEFAULTS = {
    'Explore': 'haiku',
    }


def rewrite(tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """Return a corrected ``tool_input``, or None when no change is needed.

    Parameters
    ----------
    tool_input : dict[str, Any]
        The ``Agent`` tool input as supplied by Claude Code.

    Returns
    -------
    dict[str, Any] or None
        A replacement input dict, or None to leave the call untouched.
    """
    model = str(tool_input.get('model') or '').strip()
    normalized = model.lower()

    if not model:
        default = MODEL_DEFAULTS.get(str(tool_input.get('subagent_type') or ''))
        if default is None:
            return None
        return {**tool_input, 'model': default}

    if not normalized.startswith('opus'):
        return None

    return {key: value for key, value in tool_input.items() if key != 'model'}


def main() -> None:
    """Read the hook payload on stdin and emit an updated input when required.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    if hook_input.get('tool_name') != 'Agent':
        return

    tool_input = hook_input.get('tool_input') or {}
    updated = rewrite(tool_input)
    if updated is None:
        return

    json.dump({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'allow',
            'updatedInput': updated,
            },
        }, sys.stdout)


if __name__ == '__main__':
    main()
