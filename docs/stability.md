# Stability and maintenance policy

* Pre-1.0 SemVer: PATCH releases only fix, MINOR releases may change the
  public API with the migration named in [CHANGELOG.md](../CHANGELOG.md).
  Nothing changes silently.
* Deprecated APIs keep working and warn for at least one MINOR release
  before removal.
* The bar for 1.0, stated in advance: months of green weekly canaries and
  external production users. (The maker path joined the FAK paths in the
  production-proven column on 2026-07-04; receipt in the README.)
* Bus-factor honesty: one maintainer, who trades real money through this
  exact code daily (strongest available incentive to keep it correct). The
  mitigations are structural, not promises: five small modules, the
  executable fill-contract test table, a weekly canary that opens issues by
  itself, SHA-pinned CI. Operational rule: if the canary badge goes red and
  stays red, treat the project as unmaintained and pin your last known-good
  version.
* Help wanted, precisely scoped: production receipts for `signature_type`
  1 and 2 accounts (legacy Magic/email and browser-wallet proxies). Both
  paths are introspection-tested but have never carried real money through
  this library; the maintainer's own accounts are all types 0 and 3.
