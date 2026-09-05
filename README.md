# agent-scope

Six subagent tiers that pin model and effort, a PreToolUse gate that caps
Opus fan-out per review round, and the directives that pick a tier.

## Install

```
/plugin marketplace add bissli/claude-agent-scope
/plugin install agent-scope@agent-scope
```

The first command registers this repo as a plugin source (a "marketplace");
the second installs the six tier agents, the two hooks, and the two
directives from it, and the next session runs them.

The hooks need `python3` (3.10 or later) on `PATH` and run under `sh`, so
the plugin runs on Linux and macOS; the Opus tiers pin `claude-opus-5`, so
the account needs access to that model. Without `python3` the hook commands
fail and Claude Code continues: the directives do not load, no launch is
gated, and nothing announces it. On Windows the six tier agents register
and the hooks do not run.

## What changes in a session

- Six subagent types appear, `agent-scope:haiku` through
  `agent-scope:opus-xhigh`; their descriptions add about 2.7KB to the
  `Agent` tool listing every turn.
- Two directive files, 3.4KB and 4.8KB, print into context at session
  start and again after every compaction, clear, and resume.
- An `Agent` or `Workflow` call that breaks a rule is blocked with a reason
  that names the fix:

```
review-gate: the tiers are the agent-scope plugin's agents; name agent-scope:haiku, not haiku.
```

## The problem

The main loop re-reads the whole conversation at its model's price every
turn, and a subagent that names no model inherits that model and the
session's effort. A prose rule about how many Opus reviewers a round may
hold drifts toward more of them, because nothing enforces it.

This plugin pins each subagent tier to one model and one effort level, and
moves the Opus-per-round cap from a request into a hook: the gate blocks a
launch past the cap and names the fix.

## What it ships

| Component                 | Path                                  | What it does                                                                                                  |
| ------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Six tier definitions      | `agents/*.md`                         | Pin model and effort; registered as `agent-scope:<tier>`                                                      |
| Model pin hook            | `scripts/pin-subagent-model.py`       | Strips bare `opus` alias so the frontmatter pin wins; pins `Explore` to haiku when no model is named          |
| Review gate               | `scripts/review-gate.py`              | PreToolUse hook on `Agent` and `Workflow`; caps Opus agents per round; requires a header on every Opus launch |
| Model-selection directive | `directives/agent-model-selection.md` | Injected at session start; the two questions that pick a tier                                                 |
| Review-sizing directive   | `directives/review-sizing.md`         | Injected at session start; how to size a review round and write the header                                    |
| Hook wiring               | `hooks/hooks.json`                    | Wires the four hook commands: two PreToolUse, two SessionStart                                                |

## The tiers

Claude Code registers plugin agents as `<plugin>:<agent>`, so a launch
names `agent-scope:opus-high`, never `opus-high`. The Opus tiers pin the
full model id `claude-opus-5`, so the account needs access to that model.
The sonnet and haiku tiers name the family alias, which resolves to the
account's default for that family or to `ANTHROPIC_DEFAULT_SONNET_MODEL`
and `ANTHROPIC_DEFAULT_HAIKU_MODEL` where those are set.

| Type                        | Model         | Effort | Use                                                                            |
| --------------------------- | ------------- | ------ | ------------------------------------------------------------------------------ |
| `agent-scope:haiku`         | haiku         | -      | Search, grep fan-out, classification, throwaway output                         |
| `agent-scope:sonnet-medium` | sonnet        | medium | Checklist sweeps, scripted checks, bulk edits, extraction                      |
| `agent-scope:sonnet-high`   | sonnet        | high   | Code, tests, edits, single-module debugging, review coverage past the Opus cap |
| `agent-scope:opus-medium`   | claude-opus-5 | medium | Verify with handed claims and lines; mechanical Opus checks                    |
| `agent-scope:opus-high`     | claude-opus-5 | high   | Review, multi-file debugging, synthesis; the default Opus tier                 |
| `agent-scope:opus-xhigh`    | claude-opus-5 | xhigh  | Derivation tasks; `derive:` required in header                                 |

## Choosing a tier

Two questions select the tier. Take the lowest answer to each that fits.
A verdict on a claim is always Opus, regardless of the other answers.

**Model - what the agent produces:**

- `haiku` looks up: search, grep fan-out, classification, log or dump
  summary. `Explore` is its read-only form when the file set is unknown.
- `sonnet` executes: code, tests, or edits from a brief; a cause inside
  one module; a check or sweep with the pattern given.
- `opus` judges: a review, a verdict on a claim, a cause across files,
  a synthesis.

**Effort - where the oracle lives:**

- `medium`: the brief names the oracle and the items; the agent applies
  one to the other and reports.
- `high`: the oracle is outside the agent (spec, schema, callers, test
  run); the agent adds what the brief did not contain.
- `xhigh`: no oracle exists outside the agent; the derivation is the
  oracle. The `derive:` field names the kind. A task that is merely hard,
  large, or sensitive is `high`.

Haiku takes no effort level. Sonnet stops at high.

## The review gate

`review-gate.py` runs as a PreToolUse hook on every `Agent` and `Workflow`
call. Every Opus-tier launch must open its prompt with this header, one
field per line:

```
<review-gate>
round: review|verify|synthesize|swarm
opus-cap: 3|6|9
derive: formula|bound|proof|equivalence|interleaving|joint-behavior
</review-gate>
```

`round` goes on every Opus launch; `opus-cap` on every `review`, `verify`,
or `swarm` launch; `derive` on `opus-xhigh` alone. Opus work outside a
review declares `round: swarm`.

The first counted Opus `review`, `verify`, or `swarm` agent of a cycle
fixes `opus-cap` for that cycle. The `synthesize` round holds at most two
agents per cycle at any cap.

| `opus-cap` | Declared when                              | `opus-xhigh` seats per cycle |
| ---------- | ------------------------------------------ | ---------------------------- |
| `3`        | Up to 3 briefs, none deriving; the default | 0                            |
| `6`        | 4 to 6 briefs, or one deriving brief       | 1                            |
| `9`        | 7 to 9 briefs, or two deriving briefs      | 2                            |

File count, diff size, security subject matter, importance, and the
ultracode setting never raise the cap.

A denial blocks the call, takes no slot, and names the fix. A launch on
the bare name `haiku` gets:

```
review-gate: the tiers are the agent-scope plugin's agents; name agent-scope:haiku, not haiku.
```

The gate also denies a `fable` model, `general-purpose`, an omitted type,
a prefixed name that is not one of the six tiers, an Opus launch with no
header, and a Workflow stage that carries a `model` or `effort` option
beside its `agentType`. `Plan` and a `fork` run on the main-loop model, so
the gate counts them as Opus-tier and requires the header; `Explore` with
no `model` is uncapped.

In a `Workflow` script an Opus stage is one `agent()` call at the top level,
a thunk in `parallel([...])`, or a `.then()`, `.catch()`, or `.finally()`
continuation, with the header as the first text of its prompt literal. A
stage inside `pipeline()`, a loop, or any other function is denied, as is a
saved workflow name, since the gate reads the script text.

State lives in `~/.claude/cache/review-gate/`: one `<session>.json` of
per-cycle counters, pruned to the last eight cycles, and `gate.jsonl`, an
append-only log of every launch with its type, round, cap, decision, and
description label, which grows until deleted. The environment variable
`REVIEW_GATE_HOME` overrides the path.

## What the directives say

Two `SessionStart` hook commands print two Markdown files into context at
session start, and again after a compaction, a clear, or a resume.

`directives/agent-model-selection.md` states the two questions above and
adds a dispatch table:

| Situation                                   | Action                                |
| ------------------------------------------- | ------------------------------------- |
| Output large or throwaway (logs, test spew) | Delegate, or pipe to a file then grep |
| Single-fact lookup, file/symbol known       | Do it inline                          |
| Independent chunks of work                  | One agent each, in parallel           |
| Reasoning-critical, needs full context      | Keep inline                           |

It also states that `general-purpose`, an omitted type, and a tier name
without the `agent-scope:` prefix are denied.

`directives/review-sizing.md` states how to size a review round: write
briefs first, then merge every pair one agent could hold in one read; the
remaining count is the Opus reviewer count. It repeats the header block
and the `opus-cap` table, and adds rules for handling text under review:
hand each agent the exact line ranges, freeze the text once launched, and
deliver only findings that change what the reader does.

## Update

```
/plugin update agent-scope@agent-scope
```

The plugin runs from a copy under `~/.claude/plugins/cache/` taken at
install time; an update fetches the repo again when `plugin.json` carries a
new version, and the next session runs it.

## Uninstall

```
/plugin uninstall agent-scope@agent-scope
/plugin marketplace remove agent-scope
```

The state directory `~/.claude/cache/review-gate/` outlives the plugin and
is safe to delete.

## Development

```bash
poetry install
poetry run pytest tests
claude --plugin-dir .
claude plugin validate .claude-plugin/plugin.json
```

`bump2version patch|minor|major` moves the version in `plugin.json` and tags
the commit `v<version>`.

## License

MIT
