"""pmq exceptions. The taxonomy matters: callers must be able to tell a clean
rejection (order does not exist) from an unknown outcome (order MAY exist)."""


class PmqError(Exception):
    """Base class for pmq errors."""


class OrderUncertain(PmqError):
    """An order was sent but its outcome is unknown (timeout, 5xx, exception
    after send). It MAY exist server-side. The caller must cancel, reconcile
    against exchange truth (get_trades) and stop trading that market until
    reconciled. Never book anything on this exception."""


class IntrospectionMismatch(PmqError):
    """The installed py-clob-client-v2 no longer matches the API surface pmq
    was verified against. Refusing to trade is the only safe behavior: a
    silently changed parameter meaning can turn guards into no-ops."""
