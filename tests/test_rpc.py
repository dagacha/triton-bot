"""Tests for runtime RPC configuration."""

import os
from unittest.mock import MagicMock, patch

from operate.operate_types import Chain

from triton.rpc import configure_runtime_rpcs


class TestConfigureRuntimeRpcs:
    """Tests for configure_runtime_rpcs."""

    @patch.dict(os.environ, {"GNOSIS_RPC": "https://rpc.example"}, clear=True)
    @patch.dict("triton.rpc.DEFAULT_RPCS", {Chain.GNOSIS: "https://old.example"})
    @patch.dict("triton.rpc.DEFAULT_LEDGER_APIS", {Chain.GNOSIS: MagicMock()})
    def test_updates_gnosis_rpc_and_clears_cache(self):
        """Configured Gnosis RPC should replace the default and clear caches."""
        configure_runtime_rpcs()

        from triton.rpc import DEFAULT_LEDGER_APIS, DEFAULT_RPCS

        assert DEFAULT_RPCS[Chain.GNOSIS] == "https://rpc.example"
        assert Chain.GNOSIS not in DEFAULT_LEDGER_APIS

    @patch.dict(os.environ, {}, clear=True)
    @patch.dict("triton.rpc.DEFAULT_RPCS", {Chain.GNOSIS: "https://old.example"})
    @patch.dict("triton.rpc.DEFAULT_LEDGER_APIS", {Chain.GNOSIS: MagicMock()})
    def test_noop_without_env_var(self):
        """No changes should be made when GNOSIS_RPC is unset."""
        configure_runtime_rpcs()

        from triton.rpc import DEFAULT_LEDGER_APIS, DEFAULT_RPCS

        assert DEFAULT_RPCS[Chain.GNOSIS] == "https://old.example"
        assert Chain.GNOSIS in DEFAULT_LEDGER_APIS
