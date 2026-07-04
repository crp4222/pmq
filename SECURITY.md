# Security

## Posture

* Your private key is read from the environment, used to instantiate the
  local signer, and never logged, transmitted or stored by pmq. There is no
  backend, no telemetry, no custody. The only hosts contacted are Polymarket
  endpoints (clob/gamma/data-api) and, for the on-chain debug helpers you run
  yourself, the RPC you choose.
* The builder code embedded by default is attribution metadata inside the
  signed order (public on-chain either way). It carries 0/0 commission and is
  disabled with `builder_code=None`. It cannot access funds.
* The executor refuses to trade if the installed py-clob-client-v2 no longer
  matches the API surface pmq was verified against (introspection at startup),
  rather than signing through changed semantics.

## Reading the source before trusting it

A documented wave of fake "polymarket bot" repositories steals private keys.
pmq is deliberately small (five modules) so you can audit the entire execution
path in minutes. Grep targets that settle the important questions fast:
`POLY_PRIVATE_KEY` (read once, passed to the official client), `builder_code`
(the disclosure and the opt-out), `http` (every host contacted).

## Automated watch

* Weekly canary CI runs the egress test (every DNS resolution during a full
  session must stay inside polymarket.com) and `pip-audit` over the
  dependency tree; any failure opens a labeled issue automatically.
* Dependabot files weekly update PRs for Python dependencies and for the
  GitHub Actions, which are pinned by commit SHA.
* PyPI releases carry signed PEP 740 attestations (see "Verify the claims
  yourself" in the README).

## Reporting a vulnerability

Open a GitHub security advisory on this repository (Security tab, "Report a
vulnerability") or an issue with the `security` label if it is not sensitive.
You will get an answer within a few days.
