# pmq: engineering invariants for agents CONTRIBUTING to this repo

(AGENTS.md in this repo is for agents USING the library; this file is for
agents EDITING it. Read both before changing code.)

## Never weaken (the product IS these properties)

1. **The fail-closed fill contract**: a `Fill` books only what the exchange
   confirmed (`orderID` + `success is not False` + matched amounts); 4xx is a
   clean rejection; timeout/5xx raises `OrderUncertain`; unparseable = zero.
   Any change that books more optimistically is a regression by definition,
   whatever it fixes elsewhere. Matched amounts must be finite and
   non-negative (json.loads accepts NaN/Infinity; hostile values book zero).
   `reconcile()` must keep meaning cancel + `get_trades` truth. The
   hypothesis fuzz suite (tests/test_fill_fuzz.py) pins all of this with
   generated adversarial responses: extend it with every parser change,
   never delete it.
2. **Startup introspection** (`_EXPECTED_METHODS`/`_EXPECTED_MARKET_ARGS`):
   the executor REFUSES to run on a drifted py-clob-client-v2. When bumping
   the client dependency, re-verify signatures by introspection and update
   the tables in the same commit.
3. **Builder code policy**: default = maintainer's code, defined in exactly
   ONE place (`DEFAULT_BUILDER_CODE` in executor.py) and applied
   automatically by every order path. DISCLOSED in README and code comment.
   The one-line opt-out (`builder_code=None` / env) EXISTS in code and
   stays, but is deliberately not advertised outside the code comment
   (owner decision 2026-07-04): do not re-add opt-out instructions to the
   README or other marketing surfaces, and never remove the disclosure
   itself. AND the mirror rule: keep the
   disclosure at the DOCUMENTATION level only; do not surface attribution in
   runtime channels (server startup logs, MCP tools or instructions, order
   responses). It is public on-chain in every signed order; in-band
   reminders just prompt sessions to toggle a setting that costs users
   nothing. This is the trust model (JKorf pattern).
4. **No strategy content, ever**: the maintainer's private bot strategy
   (bands, timing, hours, families, sizing) must never appear in code, docs,
   tests, commits or issues. The bot-template ships deliberately naive
   demos only.
5. **Claims must be falsifiable**: no superlatives in README/docs; dated
   claims with evidence (comparison table, on-chain receipts, measured
   studies). If you cannot prove it, do not write it.
6. **MCP safety gates**: trading tools are REGISTERED only when the operator
   sets `PMQ_MCP_LIVE=1`; per-order `PMQ_MCP_MAX_USD` and per-UTC-day
   `PMQ_MCP_DAILY_USD` caps enforced BEFORE any client call (uncertain
   outcomes consume budget conservatively). Read tools must keep working
   with zero credentials. The README tool and rails tables are part of the
   contract: keep them in sync with the registered tools.

## Working rules

* Tests green (`pytest -q`) and `ruff check .` clean before any push;
  `pyscn check src/pmq bot-template` (complexity <= 10, no dead code)
  must stay green too; clone warnings are informational (the template
  dash deliberately duplicates helpers to stay stdlib-standalone). Add
  tests with every behavior change. Network-touching tests go to
  `tests/test_canary_live.py` behind `PMQ_CANARY=1`, never in default CI.
* Exchange rules (min size, tick, fee rate) are READ from the venue
  (`book_meta`, `fee_rate`), not hardcoded. `FEE_RATES` is a documented
  snapshot of the official schedule used for estimates.
* Releases: bump version in `pyproject.toml`, `src/pmq/__init__.py` AND
  `server.json` (both version fields), update CHANGELOG.md, push, then
  `gh release create vX.Y.Z`: PyPI publish (trusted publishing, signed
  attestations) and the MCP registry republish (mcp-publish.yml,
  github-oidc) both fire on the release event. Registry gotchas: the
  server.json description caps at 100 characters, and the version must
  exist on PyPI. PyPI name is `pmquant`, import name `pmq`: keep the
  README line explaining it.
* CLAUDE.md and CONTRIBUTING.md are THE SAME FILE by contract: after
  editing one, copy it over the other in the same commit (`cp CLAUDE.md
  CONTRIBUTING.md`). Drift between them means an agent read stale rules.
* Local guard: `git config core.hooksPath .githooks` once per clone
  enables the pre-push hook (ruff + mypy + pytest). CI is the backstop,
  but the hook catches a broken push before it lands.
* GitHub Actions stay pinned by commit SHA (dependabot bumps them); new
  workflows get an explicit least-privilege permissions block. The egress
  test and pip-audit ride the weekly canary: never move them to default CI
  (they need network) and never widen the egress allowlist beyond
  polymarket.com without updating SECURITY.md and the README section.
* The weekly canary workflow is the drift alarm: if it opens an issue, the
  fix starts by re-running the introspection against the new surface, not
  by loosening the checks.
* Keep the library small and auditable (five modules): resist adding
  dependencies; stdlib first. Anything bot-shaped belongs in bot-template/,
  not in the package.
* Style: no em-dashes and no " - " connectors anywhere (strong user rule);
  keep comments sparse and constraint-focused.
