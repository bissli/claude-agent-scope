## Agent Model Selection

The main loop runs the model named in `settings.json` and re-reads the
whole conversation at that price every turn. Delegate grunt work and
keep throwaway output (file dumps, logs) out of the main context.

Every `Agent` launch names its type and omits `model`. The types are
the eight tiers the `agent-scope` plugin ships, agent definitions that
pin model and effort, named with the plugin prefix:
`agent-scope:haiku`, `agent-scope:sonnet-medium`,
`agent-scope:sonnet-high`, `agent-scope:opus-medium`,
`agent-scope:opus-high`, `agent-scope:opus-xhigh`,
`agent-scope:fable-high`, `agent-scope:fable-xhigh` - plus `Explore`,
which runs on haiku, and `Plan`, which runs on the main-loop model.
`general-purpose`, an omitted type, and a tier name without its prefix
are denied. A `Workflow` stage names one of the eight tiers, prefix
included, as a literal `agentType`; the gate denies an unpinned stage,
any other type, and a `model` or `effort` option, which would outrank
the pin. A `fable` model option would outrank the Fable tiers' version
pin the same way, and the alias can change over time, so the gate denies
it on every type.

Two questions pick the tier. Take the lowest answer to each that fits,
with one override: a verdict on a claim is always Opus or Fable. First
the model, by what the agent produces:

- `haiku` looks up: search, grep fan-out, classification, a summary of
  a log or a dump. `Explore` is its read-only form when the file set is
  unknown.
- `sonnet` executes: code, tests, or edits from a brief; a cause inside
  one module; a check or a sweep with the pattern given.
- `opus` judges: a review, a verdict on a claim, a cause across files,
  a synthesis.
- `fable` holds what `opus` cannot: material that must be held whole,
  because any split into Opus-sized briefs changes the question.

Then the effort, by where the oracle lives - what the result is
checked against. Name it in the brief. The haiku tier ships with no
effort level. The plugin offers Sonnet at medium and high, and Fable at
high and xhigh, so a brief that fails to split runs at high even where
it hands over the oracle and the items:

- `medium`: the brief names the oracle and the items - these claims at
  these lines, these files against this pattern - and the agent adds
  nothing: it applies one to the other and reports.
- `high`: the brief names an oracle outside the agent - the spec, the
  schema, the platform's documented behavior, a convention, a test
  run, the callers - and the agent adds what the brief did not contain:
  which files matter, what the cause is, what is wrong, the code a
  brief specifies. A reader checks the addition against that oracle.
  `agent-scope:opus-high` sits here, and `agent-scope:fable-high` where
  the brief fails to split.
- `xhigh`: no oracle outside the agent settles the principal claim, so
  the agent must derive it - a formula or bound, a proof of an
  invariant, an equivalence no test can settle, an interleaving
  analysis with no reproducer, the joint behavior of separately
  designed parts. A reader checks only by deriving it too. Partial
  tests, numerical probes, and counterexamples stay useful as checks;
  their availability alone does not settle the claim. The launch names
  the kind in `derive:`; a task that is merely hard, large, or
  sensitive is `high`. Both `agent-scope:opus-xhigh` and
  `agent-scope:fable-xhigh` sit here and spend the same derive seats;
  take `opus-xhigh` unless the brief fails to split.

| Situation                                   | Action                                           |
| ------------------------------------------- | ------------------------------------------------ |
| Output large or throwaway (logs, test spew) | Delegate, or pipe to a file then grep            |
| Single-fact lookup, file/symbol known       | Do it inline (an agent costs more than it saves) |
| Independent chunks of work                  | One agent each, in parallel (one message)        |
| Reasoning-critical, needs the conversation  | Keep inline                                      |
