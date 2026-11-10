#!/usr/bin/env python3
"""PreToolUse hook that runs an Explore agent on haiku when it names no model.

Every other launch passes through untouched: each tier definition under
``agents/`` names its own family alias and effort.
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
    subagent_type = str(tool_input.get('subagent_type') or '')

    if not model:
        default = MODEL_DEFAULTS.get(subagent_type)
        if default is None:
            return None
        return {**tool_input, 'model': default}
    return None


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
