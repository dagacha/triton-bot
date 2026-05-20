"""Runtime RPC configuration helpers."""

import os

from operate.ledger import DEFAULT_LEDGER_APIS, DEFAULT_RPCS
from operate.operate_types import Chain

# Ensure env vars are loaded (constants.py is the canonical location)
from triton import constants  # noqa: F401


def configure_runtime_rpcs() -> None:
    """Apply RPC URLs from the environment to operate's runtime caches."""
    gnosis_rpc = os.getenv("GNOSIS_RPC")
    if not gnosis_rpc:
        return

    DEFAULT_RPCS[Chain.GNOSIS] = gnosis_rpc
    DEFAULT_LEDGER_APIS.pop(Chain.GNOSIS, None)
