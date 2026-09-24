#!/usr/bin/env python3
"""PreToolUse hook that lets the tier frontmatter pins outrank a bare alias.

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
- A bare alias naming a pinned tier's own family is removed there too:
  ``sonnet`` on the two Sonnet tiers, ``haiku`` on the haiku tier. Their
  frontmatter pin then wins over the account's default for that family.
- Every other type passes ``sonnet`` and ``haiku`` through. Removing one
  would run the launch on that type's own pin, such as ``claude-fable-5-1``,
  or else on the main-loop model, while ``review-gate.py`` counts it as an
  uncapped launch.
- ``fable`` passes through untouched. Passing it through is not a grant.
  ``review-gate.py`` runs beside this hook, its deny outranks this hook's
  allow, and it denies ``fable`` on every type, since an invocation-level
  model outranks the version pin in the Fable definitions under
  ``agents/``.
- ``Explore`` is pinned to ``haiku`` when the caller names no model, keeping
  grep-fanout work off the expensive main-loop model.
"""

import json
import sys
from typing import Any

MODEL_DEFAULTS = {
    'Explore': 'haiku',
    }
FAMILY_BY_PINNED_TIER = {
    'agent-scope:sonnet-medium': 'sonnet',
    'agent-scope:sonnet-high': 'sonnet',
    'agent-scope:haiku': 'haiku',
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
    subagent_type = str(tool_input.get('subagent_type') or '')

    if not model:
        default = MODEL_DEFAULTS.get(subagent_type)
        if default is None:
            return None
        return {**tool_input, 'model': default}

    if (normalized.startswith('opus')
        or normalized == FAMILY_BY_PINNED_TIER.get(subagent_type.lower())):
        return {key: value for key, value in tool_input.items() if key != 'model'}
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
