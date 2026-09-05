## Review Sizing

A mechanical edit may need no review. Review work, and every Opus
launch, runs marked: an `Agent` launch or a `Workflow` stage; one
stage is one launch.

Write the briefs first, then merge every pair one agent could hold in
one read; what remains is the Opus reviewer count. Overlap survives the
merge only when independent agreement is the evidence. Breadth past
that comes from `agent-scope:sonnet-high` and `agent-scope:haiku`, which
are uncapped and return claims, never verdicts: the re-check below judges
them, or an Opus `verify` agent does and takes a slot.

`opus-cap` is the Opus ceiling per round, declared from the merged Opus
briefs of the first round launched. The first Opus `review`, `verify`,
or `swarm` agent fixes it for the cycle, and no later agent can change
it; the cap also sets the cycle's `opus-xhigh` seats:

| `opus-cap` | Declared when                              | `opus-xhigh` seats per cycle |
| ---------- | ------------------------------------------ | ---------------------------- |
| `3`        | up to 3 briefs, none deriving; the default | 0                            |
| `6`        | 4 to 6 briefs, or one deriving brief       | 1                            |
| `9`        | 7 to 9 briefs, or two deriving briefs      | 2                            |

File count, diff size, security subject matter, importance, and the
ultracode setting never raise the cap, and none of them is a derive.
An `opus-xhigh` seat needs `derive:` naming what the agent derives -
`formula`, `bound`, `proof`, `equivalence`, `interleaving`, or
`joint-behavior`; the one test is that anything outside the agent
that checks the result makes the brief `opus-high`. Only Opus-tier
agents take slots: the three Opus tiers, and any type that runs on the
main-loop model, `Plan` and a `fork` whatever `model` it names among
them.

A cycle is every marked agent launched for one user prompt; the next
prompt starts a new cycle. The rounds are types, not a pipeline; a
review may end after `review`. Size `verify` to the surviving claims,
never one agent per finding: `agent-scope:opus-medium` when it is handed
the claims and the lines, `agent-scope:opus-high` when it must build its
own test. A `verify` round that needs more than the cap merges claims or
moves the overflow to `agent-scope:sonnet-high`. `synthesize` is one Opus
agent after the other rounds report, at most two per cycle.

Every Opus agent, and every review, verify, or synthesize agent on any
tier, opens its prompt with this header, one field per line. Opus work
outside a review declares `round: swarm`, a fourth round counted and
capped like `review` on its own counter. `opus-cap` goes on every
Opus `review`, `verify`, or `swarm` agent and carries the cycle's one
value; `derive` goes on an `opus-xhigh` agent alone, in every round,
`synthesize` included:

    <review-gate>
    round: review|verify|synthesize|swarm
    opus-cap: 3|6|9
    derive: formula|bound|proof|equivalence|interleaving|joint-behavior
    </review-gate>

`review-gate.py` enforces the caps and the header on `Agent` and
`Workflow`; a denial takes no slot and names the fix. In a `Workflow`
the header is the first text inside each Opus stage's own prompt
literal; a variable, a concatenation, a helper call, or a leading
`${...}` hides it, and only the text before the first `${` is read:

    agent(`<review-gate>
    round: review
    opus-cap: 3
    </review-gate>
    ${brief}`, {agentType: 'agent-scope:opus-high', label: 'review:bugs'})

An Opus stage is one `agent()` call at the top level, a thunk in
`parallel([...])`, or a `.then()`, `.catch()`, or `.finally()`
continuation; inside `pipeline()`, a mapped callback, a loop, or any
other function it is denied, so fan-out runs on `agent-scope:sonnet-high`
or `agent-scope:haiku`. A script's stages take slots together, on the
counters `Agent` launches use.

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
  report. A kill frees no slot, so a relaunch must fit what the round
  has left.
- One review at a time. Stop a round before relaunching one that
  answers the same question.
- Re-check every surviving finding against the file before acting on
  it or asserting it: agents correct the author's claims and overstate
  their own headline alike.
- Deliver only the findings that change what the reader does. Rank,
  cut, park the rest.
