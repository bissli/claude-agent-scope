## Agent Model Selection

The main loop runs the model named in `settings.json` and re-reads the
whole conversation at that price every turn. Delegate grunt work and
keep throwaway output out of the main context.

| Situation                                   | Action                                           |
| ------------------------------------------- | ------------------------------------------------ |
| Output large or throwaway (logs, test spew) | Delegate, or pipe to a file then grep            |
| Single-fact lookup, file/symbol known       | Do it inline (an agent costs more than it saves) |
| Independent chunks of work                  | One agent each, in parallel (one message)        |
| Reasoning-critical, needs the conversation  | Keep inline                                      |

Every `Agent` launch names its type and omits `model`. The types are
the eight tiers the `agent-scope` plugin ships, agent definitions that
pin model and effort, named with the plugin prefix:
`agent-scope:haiku`, `agent-scope:sonnet-medium`,
`agent-scope:sonnet-high`, `agent-scope:opus-medium`,
`agent-scope:opus-high`, `agent-scope:opus-xhigh`,
`agent-scope:fable-high`, `agent-scope:fable-xhigh` - plus `Explore`,
which runs on haiku, and `Plan`, which runs on the main-loop model.

Two questions pick the tier. Take the lowest answer to each that fits,
with one override: a verdict on a claim - whether a reported finding
holds - is always Opus or Fable. First the model, by what the agent
produces:

- `haiku` looks up: search, grep fan-out, classification, a summary of
  a log or a dump. `Explore` is its read-only form when the file set is
  unknown.
- `sonnet` executes: code, tests, or edits from a brief; a cause inside
  one module; a check or a sweep with the pattern given.
- `opus` judges: a review, a verdict on a claim, a cause across files,
  a synthesis.
- `fable` holds what `opus` cannot: material that must be held whole,
  because any split into briefs one Opus agent can hold changes the
  question.

Then the effort, by where the oracle lives - what the result is
checked against. Name it in the brief. The haiku tier ships with no
effort level. The plugin offers Sonnet at medium and high, Opus at
medium, high, and xhigh, and Fable at high and xhigh. Fable has no
medium rung, so a brief that fails to split is `agent-scope:fable-high`
even where it names the oracle and the items:

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
  invariant, an equivalence no test distinguishes, an interleaving with
  no reproducer, the joint behavior of separately designed subsystems.
  A reader checks only by deriving it too. Partial tests, numerical
  probes, and counterexamples stay useful as checks. Their availability
  alone does not settle the claim. A task that is merely hard, large,
  or sensitive is `high`. Both `agent-scope:opus-xhigh` and
  `agent-scope:fable-xhigh` sit here. Take `opus-xhigh` unless the
  brief fails to split.

The gate denies `general-purpose`, an omitted type, a tier name without
its prefix, and a `fable` model option on any type: that option outranks
the Fable tiers' version pins, and the `fable` alias can change. A
`Workflow` stage names one of the eight tiers as a literal `agentType`,
and the gate denies an unpinned stage.
