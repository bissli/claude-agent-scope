## Review Sizing

A mechanical edit may need no review. Review work, and every capped
launch, runs marked: an `Agent` launch or a `Workflow` stage; one
stage is one launch.

Three terms carry the accounting. A *capped tier* is
`agent-scope:opus-medium`, `agent-scope:opus-high`,
`agent-scope:opus-xhigh`, or `agent-scope:fable-xhigh`. A *deriving
tier* is `agent-scope:opus-xhigh` or `agent-scope:fable-xhigh`. A
*capped launch* is one on a capped tier, or on any type that runs on
the main-loop model - `Plan` and a `fork` whatever `model` it names
among them.

Write the briefs first, then merge every pair one agent could hold in
one read; what remains is the capped launch count. Overlap survives the
merge only when independent agreement is the evidence. Sonnet and Haiku
tiers are uncapped; coverage past that count goes to
`agent-scope:sonnet-high` or `agent-scope:haiku`, which return claims,
never verdicts: the re-check below judges them, or a capped `verify`
agent does and consumes a capped launch.

`opus-cap` is the ceiling on capped launches per round, declared from
the merged capped briefs of the first round launched. The first capped
`review`, `verify`, or `swarm` launch fixes it for the cycle, and no
later launch can change it; the cap also sets the cycle's derive seats,
which `opus-xhigh` and `fable-xhigh` spend from together:

| `opus-cap` | Declared when                              | derive seats per cycle |
| ---------- | ------------------------------------------ | ---------------------- |
| `3`        | up to 3 briefs, none deriving; the default | 0                      |
| `6`        | 4 to 6 briefs, or one deriving brief       | 1                      |
| `9`        | 7 to 9 briefs, or two deriving briefs      | 2                      |

A cycle's deriving demand goes into the value its first capped `review`,
`verify`, or `swarm` launch declares. A cap fixed at `3` seats no
derivation for the rest of the cycle. File count, diff size, security
subject matter, importance, and the ultracode setting never raise the
cap, and none of them is a derive. A derive seat needs `derive:` naming
what the agent derives - `formula`, `bound`, `proof`, `equivalence`,
`interleaving`, or `joint-behavior`; the one test: if an oracle outside
the agent settles the principal claim, the brief is `opus-high`. Partial
tests, numerical probes, and counterexamples stay useful as checks;
their availability alone does not settle the principal claim.

A cycle is every marked agent launched for one user prompt; the next
prompt starts a new cycle. The rounds are types, not a pipeline; a
review may end after `review`. Size `verify` to the surviving claims,
never one agent per finding: `agent-scope:opus-medium` when it is handed
the claims and the lines, `agent-scope:opus-high` when it must build its
own test. A `verify` round that needs more than the cap merges claims or
moves the overflow to `agent-scope:sonnet-high`. `synthesize` is one
capped agent after the other rounds report, at most two per cycle;
coverage past that goes to `agent-scope:sonnet-high`, which returns
claims for that synthesizer to judge.

Every capped launch, and every review, verify, or synthesize agent on
any tier, opens its prompt with this header, one field per line. Capped
work outside a review declares `round: swarm`, a fourth round counted
and capped like `review` on its own counter. `opus-cap` goes on every
capped `review`, `verify`, or `swarm` launch and carries the cycle's one
value; `derive` goes on a deriving tier alone, in every round,
`synthesize` included:

    <review-gate>
    round: review|verify|synthesize|swarm
    opus-cap: 3|6|9
    derive: formula|bound|proof|equivalence|interleaving|joint-behavior
    </review-gate>

`review-gate.py` enforces the caps and the header on `Agent` and
`Workflow`; a denial consumes no capped launch and names the fix. In a
`Workflow` the header is the first text inside each capped stage's own
prompt literal; a variable, a concatenation, a helper call, or a leading
`${...}` hides it, and only the text before the first `${` is read:

    agent(`<review-gate>
    round: review
    opus-cap: 3
    </review-gate>
    ${brief}`, {agentType: 'agent-scope:opus-high', label: 'review:bugs'})

A capped stage is one `agent()` call at the top level, a thunk in
`parallel([...])`, or a `.then()`, `.catch()`, or `.finally()`
continuation; inside `pipeline()`, a mapped callback, a loop, or any
other function it is denied, so fan-out runs on
`agent-scope:sonnet-high` or `agent-scope:haiku`. Each stage names its
tier as a literal `agentType` and carries no `model` or `effort`
option. A script's capped stages reserve together, on the counters
`Agent` launches use, and the batch is refused whole: a script whose
last stage overruns a cap moves no counter at all.

Hold the text once launched:

- Hand each agent the exact text: the plaintext file and the line
  ranges this session changed, or the excerpt inline. A bare path to a
  long file sends the agent reading in chunks until it stalls. Never a
  `git diff` - agents count the `+` and report diff offsets as line
  numbers. A rewrapped paragraph gets the range of the words that
  changed, not the paragraph.
- Freeze the text under review. A rewrite after launch - the author's
  or the formatter's - invalidates the review. Keep a `git diff`
  snapshot of the paths under review; hold the edit until the agents
  report. The budgets are cumulative, not concurrency slots: a launch
  frees nothing when it finishes, fails, is killed, is interrupted, or
  is lost with the session, so a relaunch must fit what the round has
  left.
- One review at a time. Stop a round before relaunching one that
  answers the same question.
- Re-check every surviving finding against the file before acting on
  it or asserting it: agents correct the author's claims and overstate
  their own headline alike.
- Deliver only the findings that change what the reader does. Rank,
  cut, park the rest.
