# agent-scope

Eight subagent tiers that pin model and effort, uncapped Sonnet and Haiku
delegation, a PreToolUse gate that budgets Opus and Fable launches per round
and shares derive seats between them, and the directives that pick a tier.

## Install

```
/plugin marketplace add bissli/claude-agent-scope
/plugin install agent-scope@agent-scope
```

The first command registers this repo as a plugin source (a "marketplace");
the second installs the eight tier agents, the two hooks, and the two
directives from it, and the next session runs them.

## Requirements

| Requirement                  | Applies to                         |
| ---------------------------- | ---------------------------------- |
| `python3` 3.10 or later      | Both hooks                         |
| Claude Code 2.1.219+         | The Opus tiers and the review gate |
| Claude Code 2.1.255+         | The two Fable tiers                |
| Access to `claude-opus-5`    | The three Opus tiers               |
| Access to `claude-fable-5-1` | The two Fable tiers                |

The hooks run through `sh`, so Linux and macOS execute them. On Windows the
eight tier agents register and the hooks do not run. Without `python3` on
`PATH` the two PreToolUse commands fail and Claude Code continues: no launch is
gated, and nothing announces it. The directives still print, since `cat`
injects them.

An account without Fable access cannot launch a Fable tier; the launch fails
at the model, and no tier substitutes for it. The other six tiers are
unaffected. The 0.2.x releases pin the model ids the Anthropic API uses and
run on Claude Code 2.1.263. A Bedrock or Vertex deployment names its models
differently, so the pinned ids need a mapping to that provider's names before
the Opus and Fable tiers resolve.

## What changes in a session

- Eight subagent types appear, `agent-scope:haiku` through
  `agent-scope:fable-xhigh`; their names and descriptions are 4.8KB of source
  text in the `Agent` tool listing every turn, source bytes rather than tokens
  or cost.
- Two directive files, 4.0KB and 5.9KB of source text, print into context at
  session start and again after every compaction, clear, and resume.
- An `Agent` or `Workflow` call that breaks a rule is blocked with a reason
  that names the fix:

```
review-gate: the tiers are the agent-scope plugin's agents; name agent-scope:haiku, not haiku.
```

## The problem

The main loop re-reads the whole conversation at its model's price every
turn, and a subagent that names no model inherits that model and the
session's effort. A prose rule about how many capped reviewers a round may
hold drifts toward more of them, because nothing enforces it.

This plugin pins each subagent tier to one model and one effort level, and
moves the per-round budget from a request into a hook: the gate blocks a
launch past the budget and names the fix.

## What it ships

| Component                 | Path                                  | What it does                                                                                                                |
| ------------------------- | ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Eight tier definitions    | `agents/*.md`                         | Pin model and effort; registered as `agent-scope:<tier>`                                                                    |
| Model pin hook            | `scripts/pin-subagent-model.py`       | Strips bare `opus` alias so the frontmatter pin wins; pins `Explore` to haiku when no model is named                        |
| Review gate               | `scripts/review-gate.py`              | PreToolUse hook on `Agent` and `Workflow`; caps Opus and Fable launches per round; requires a header on every capped launch |
| Model-selection directive | `directives/agent-model-selection.md` | Injected at session start; the two questions that pick a tier                                                               |
| Review-sizing directive   | `directives/review-sizing.md`         | Injected at session start; how to size a review round and write the header                                                  |
| Hook wiring               | `hooks/hooks.json`                    | Wires four hook commands: two PreToolUse scripts and two SessionStart directive injections                                  |

## The tiers

Claude Code registers plugin agents as `<plugin>:<agent>`, so a launch
names `agent-scope:opus-high`, never `opus-high`. The Opus tiers pin the
full model id `claude-opus-5` and the Fable tiers pin `claude-fable-5-1`, so
the account needs access to those models. The sonnet and haiku tiers name the
family alias, which resolves to the account's default for that family or to
`ANTHROPIC_DEFAULT_SONNET_MODEL` and `ANTHROPIC_DEFAULT_HAIKU_MODEL` where
those are set.

| Type                        | Model            | Effort | Budget               | Use                                                                                 |
| --------------------------- | ---------------- | ------ | -------------------- | ----------------------------------------------------------------------------------- |
| `agent-scope:haiku`         | haiku            | -      | uncapped             | Search, grep fan-out, classification, throwaway output                              |
| `agent-scope:sonnet-medium` | sonnet           | medium | uncapped             | Checklist sweeps, scripted checks, bulk edits, extraction                           |
| `agent-scope:sonnet-high`   | sonnet           | high   | uncapped             | Code, tests, edits, single-module debugging, review coverage past the capped budget |
| `agent-scope:opus-medium`   | claude-opus-5    | medium | capped launch        | Verify with handed claims and lines; mechanical Opus checks                         |
| `agent-scope:opus-high`     | claude-opus-5    | high   | capped launch        | Review, multi-file debugging, synthesis; the default Opus tier                      |
| `agent-scope:opus-xhigh`    | claude-opus-5    | xhigh  | capped launch + seat | Derivation tasks; `derive:` required in header                                      |
| `agent-scope:fable-high`    | claude-fable-5-1 | high   | capped launch        | Review, verdict, or synthesis on material that will not split; no `derive:`         |
| `agent-scope:fable-xhigh`   | claude-fable-5-1 | xhigh  | capped launch + seat | A derivation that will not split; `derive:` required in header                      |

A *capped tier* is one of the three Opus definitions or the two Fable ones; a
*deriving tier* is `opus-xhigh` or `fable-xhigh`. A launch on a deriving tier
spends both a capped launch and one of the cycle's derive seats, which the two
deriving tiers share. Sonnet and Haiku launches spend neither, in any quantity.

## Choosing a tier

Two questions select the tier. Take the lowest answer to each that fits.
A verdict on a claim is always Opus or Fable, regardless of the other answers.

**Model - what the agent produces:**

- `haiku` looks up: search, grep fan-out, classification, log or dump
  summary. `Explore` is its read-only form when the file set is unknown.
- `sonnet` executes: code, tests, or edits from a brief; a cause inside
  one module; a check or sweep with the pattern given.
- `opus` judges: a review, a verdict on a claim, a cause across files,
  a synthesis.
- `fable` holds what `opus` cannot: material that must be held whole,
  because any split into Opus-sized briefs changes the question.

**Effort - where the oracle lives:**

- `medium`: the brief names the oracle and the items; the agent applies
  one to the other and reports.
- `high`: the oracle is outside the agent (spec, schema, callers, test
  run); the agent adds what the brief did not contain. `opus-high` sits
  here, and `fable-high` where the brief fails to split.
- `xhigh`: no oracle outside the agent settles the principal claim, so the
  agent must derive it, and a reader checks only by deriving it too. The
  `derive:` field names the kind, on `opus-xhigh` and `fable-xhigh` alike, and
  the two spend the same seats. Partial tests, numerical probes, and
  counterexamples stay useful as checks; their availability alone does not
  settle the claim. A task that is merely hard, large, or sensitive is `high`.

The haiku tier ships with no effort level. The plugin offers Sonnet at medium
and high, and Fable at high and xhigh, so a brief that fails to split runs at
high even where it hands over the oracle and the items.

### What "held whole" means

The Fable tiers exist for one shape of brief. Some questions are answered by
adding up answers about the parts: review these three files, and the review of
the change is the three reviews together. Other questions live in how the parts
interact: whether a reader can see stale data depends on the order in which a
writer, a timeout, a cache, and an invalidation path run, so a reviewer who
sees two of the four cannot answer it. Material of the second kind must be held
whole.

The test is to write the split. Divide the brief into pieces one Opus agent
can each read, and write the step that combines their answers. Where the
combined answers settle the original question, the brief splits, and Opus does
it, several Opus agents if it is large. Where every division leaves the
question open, because each piece's answer depends on what the other pieces
do, the brief does not split, and one Fable agent holds it all. The directives
call this "the brief fails to split".

Size is not the test. A forty-file change whose files are independent splits
by file or by review dimension and stays Opus work. A four-module interaction
with a contract and a reproducer does not split and is `fable-high`. The same
four modules with no check able to settle the claim, so that only an argument
over every interleaving settles it, is `fable-xhigh` with `derive:
interleaving`.

`probes/routing_probe.py` is the check that this text carries the rule to a
model: it hands both directives and the eight descriptions to a model with no
other context, asks it to restate the split test, and scores its routing of
eight labeled briefs against the intended tiers.

## The review gate

`review-gate.py` runs as a PreToolUse hook on every `Agent` and `Workflow`
call. Every capped launch must open its prompt with this header, one
field per line:

```
<review-gate>
round: review|verify|synthesize|swarm
opus-cap: 3|6|9
derive: formula|bound|proof|equivalence|interleaving|joint-behavior
</review-gate>
```

`round` goes on every capped launch; `opus-cap` on every capped `review`,
`verify`, or `swarm` launch; `derive` on a deriving tier alone, in every
round, `synthesize` included. Capped work outside a review declares
`round: swarm`.

`opus-cap` names the whole capped budget, Fable included. The first counted
capped `review`, `verify`, or `swarm` launch of a cycle fixes the value for
that cycle, and no later launch may declare a different one, so a cycle
carrying a deriving brief declares 6 or 9 from that first launch. The
`synthesize` round holds at most two capped agents per cycle at any value.

| `opus-cap` | Declared when                              | derive seats per cycle |
| ---------- | ------------------------------------------ | ---------------------- |
| `3`        | Up to 3 briefs, none deriving; the default | 0                      |
| `6`        | 4 to 6 briefs, or one deriving brief       | 1                      |
| `9`        | 7 to 9 briefs, or two deriving briefs      | 2                      |

File count, diff size, security subject matter, importance, and the
ultracode setting never raise the cap.

The budgets are cumulative launch and reservation counts, not concurrency
slots. A launch that finishes, fails, is killed, is interrupted, or dies with
the session frees nothing, so a relaunch must fit what the round has left.

A denial blocks the call, takes no slot, and names the fix. A launch on
the bare name `haiku` gets:

```
review-gate: the tiers are the agent-scope plugin's agents; name agent-scope:haiku, not haiku.
```

The gate also denies a `fable` model option on any type, the two Fable
tiers included: an invocation-level model overrides the definitions' version
pins, and the `fable` family alias is configurable and can change over time.
It denies `general-purpose`, an omitted type, a prefixed name that is not one
of the eight tiers, a capped launch with no header, and a Workflow stage that
carries a `model` or `effort` option beside its `agentType`. `Plan` and a
`fork` run on the main-loop model, so the gate counts them as capped launches
and requires the header; `Explore` with no `model` is uncapped. An explicit
`sonnet` or `haiku` model keeps a launch uncapped, the Fable tiers included,
and runs that definition on the cheap model, since the pin hook rewrites
neither alias and a `model` option outranks the frontmatter pin; a `fork` is
the exception and stays counted whatever `model` it names. A launch names its
tier and omits `model`.

In a `Workflow` script a capped stage is one `agent()` call at the top level,
a thunk in `parallel([...])`, or a `.then()`, `.catch()`, or `.finally()`
continuation, with the header as the first text of its prompt literal. A
capped stage inside `pipeline()`, a loop, or any other function is denied, as
is a saved workflow name, since the gate reads the script text. A cheap stage
needs no header and may be mapped, looped, or repeated; it still names its tier
as a literal `agentType` and carries no `model` or `effort` option. One
script's capped stages reserve together and are refused whole.

State lives in `~/.claude/cache/review-gate/`: one `<session>.json` of
per-cycle counters, pruned to the last eight cycles, and `gate.jsonl`, an
append-only log of every launch with its type, round, cap, decision, and
description label, which grows until deleted. The environment variable
`REVIEW_GATE_HOME` overrides the path.

## What the pins do not cover

Frontmatter pins state what the plugin requests. The runtime can override
them, and the gate reads the request, not the result:

- `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` overrides agent definitions, the cheap
  tiers included. With the force flag alone, the main conversation model can
  become the forced model.
- `CLAUDE_CODE_EFFORT_LEVEL`, set explicitly, overrides the effort in
  frontmatter, so a tier name does not by itself establish the effort a
  subagent ran at.
- The gate's cheap-tier classification reads the type and the `model` option
  of the call. It is not a measurement of the model the runtime finally
  selected.

Reading the environment shows which of these are set:

```bash
env | grep -E 'CLAUDE_CODE_(SUBAGENT_MODEL|EFFORT_LEVEL)'
```

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
| Reasoning-critical, needs the conversation  | Keep inline                           |

It also states that `general-purpose`, an omitted type, and a tier name
without the `agent-scope:` prefix are denied.

`directives/review-sizing.md` states how to size a review round: write
briefs first, then merge every pair one agent could hold in one read; the
remaining count is the capped launch count. It defines the capped and deriving
tiers, repeats the header block and the `opus-cap` table, and adds rules for
handling text under review: hand each agent the exact line ranges, freeze the
text once launched, and deliver only findings that change what the reader
does.

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
poetry install --with dev
poetry run pytest tests
poetry run python probes/routing_probe.py --model opus --model sonnet
claude --plugin-dir .
claude plugin validate agents
claude plugin validate .claude-plugin/plugin.json
```

`claude plugin validate agents` checks the agent directory, which validating
the manifest alone does not. Neither command parses the frontmatter
strictly: both pass on a description that a YAML loader rejects. The
`tests/test_agent_definitions.py` suite is what holds the frontmatter to
real YAML, so `pytest` is the check that a strict loader still reads each
definition's model and effort.

`probes/routing_probe.py` reads the directives and descriptions as a model
does: it hands them to a model with no other context, asks it to restate the
split test in its own words and to route eight labeled briefs, prints the
restatement and the sentences the model found unclear, and exits non-zero on a
misrouted brief. Each model run is a paid call of about a quarter dollar and
one to three minutes, so the probe sits outside `pytest` and runs by hand after
a wording change.

`bump2version patch|minor|major` moves the version in `plugin.json` and tags
the commit `v<version>`.

## License

MIT
