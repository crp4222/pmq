# pmq: engineering invariants for agents CONTRIBUTING to this repo

(AGENTS.md in this repo is for agents USING the library; this file is for
agents EDITING it. Read both before changing code.)

## Never weaken (the product IS these properties)

1. **The fail-closed fill contract**: a `Fill` books only what the exchange
   confirmed (`orderID` + `success is not False` + matched amounts); 4xx is a
   clean rejection; timeout/5xx raises `OrderUncertain`; unparseable = zero.
   Any change that books more optimistically is a regression by definition,
   whatever it fixes elsewhere. `reconcile()` must keep meaning cancel +
   `get_trades` truth.
2. **Startup introspection** (`_EXPECTED_METHODS`/`_EXPECTED_MARKET_ARGS`):
   the executor REFUSES to run on a drifted py-clob-client-v2. When bumping
   the client dependency, re-verify signatures by introspection and update
   the tables in the same commit.
3. **Builder code policy**: default = maintainer's code, DISCLOSED in README
   and code comment, opt-out one line (`builder_code=None` / env). Never
   hide it, never remove the disclosure, never make opt-out harder. This is
   the trust model (JKorf pattern).
4. **No strategy content, ever**: the maintainer's private bot strategy
   (bands, timing, hours, families, sizing) must never appear in code, docs,
   tests, commits or issues. The bot-template ships deliberately naive
   demos only.
5. **Claims must be falsifiable**: no superlatives in README/docs; dated
   claims with evidence (comparison table, on-chain receipts, measured
   studies). If you cannot prove it, do not write it.
6. **MCP safety gates**: trading tools are REGISTERED only when the operator
   sets `PMQ_MCP_LIVE=1`; per-order `PMQ_MCP_MAX_USD` cap enforced before
   any client call. Read tools must keep working with zero credentials.

## Working rules

* Tests green (`pytest -q`) and `ruff check .` clean before any push;
  `pyscn check src/pmq bot-template` (complexity <= 10, no dead code)
  must stay green too; clone warnings are informational (the template
  dash deliberately duplicates helpers to stay stdlib-standalone). add
  tests with every behavior change. Network-touching tests go to
  `tests/test_canary_live.py` behind `PMQ_CANARY=1`, never in default CI.
* Exchange rules (min size, tick, fee rate) are READ from the venue
  (`book_meta`, `fee_rate`), not hardcoded. `FEE_RATES` is a documented
  snapshot of the official schedule used for estimates.
* Releases: bump version in `pyproject.toml` AND `src/pmq/__init__.py`,
  update CHANGELOG.md, push, then `gh release create vX.Y.Z`: PyPI publish
  is automatic via trusted publishing (no tokens anywhere). PyPI name is
  `pmquant`, import name `pmq`: keep the README line explaining it.
* The weekly canary workflow is the drift alarm: if it opens an issue, the
  fix starts by re-running the introspection against the new surface, not
  by loosening the checks.
* Keep the library small and auditable (five modules): resist adding
  dependencies; stdlib first. Anything bot-shaped belongs in bot-template/,
  not in the package.
* Style: no em-dashes and no " - " connectors anywhere (strong user rule);
  keep comments sparse and constraint-focused.
