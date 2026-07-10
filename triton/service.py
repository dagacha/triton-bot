"""Triton Service Module.
This module defines the TritonService class, which handles the operations of the Triton bot service.
"""

import binascii
import logging
import os
import time
import traceback
from collections.abc import Mapping
from datetime import datetime
from typing import List, Optional, Tuple, cast

from autonomy.chain.base import registry_contracts
from autonomy.chain.exceptions import ChainInteractionError, ChainTimeoutError, RPCError
from operate.cli import OperateApp
from operate.data import DATA_DIR
from operate.data.contracts.mech_activity.contract import MechActivityContract
from operate.data.contracts.requester_activity_checker.contract import (
    RequesterActivityCheckerContract,
)
from operate.ledger import get_default_ledger_api
from operate.ledger.profiles import CONTRACTS, OLAS, get_staking_contract
from operate.operate_types import Chain, LedgerType
from operate.services.protocol import StakingManager, StakingState
from operate.services.service import NON_EXISTENT_TOKEN
from operate.utils import gnosis as gnosis_utils
from requests.exceptions import ConnectionError as RequestsConnectionError
from web3.exceptions import ContractLogicError as Web3ContractLogicError

from triton.chain import (
    get_native_balance,
    get_olas_balance,
    get_staking_status,
    get_wrapped_native_balance,
)
from triton.exceptions import (
    ContractExecutionError,
    InsufficientFundsError,
    RateLimitError,
)
from triton.rpc import configure_runtime_rpcs

configure_runtime_rpcs()

SAFE_TRANSFER_FALLBACK_GAS = int(os.getenv("SAFE_TRANSFER_FALLBACK_GAS", "500000"))

_RETRYABLE_CHAIN_ERRORS = (
    "FeeTooLow",
    "ReplacementNotAllowed",
    "wrong transaction nonce",
    "OldNonce",
    "nonce too low",
)


def _normalize_gas_pricing(gas_pricing: object) -> dict:
    """Normalize gas pricing from the ledger API into tx-ready fields."""
    if not isinstance(gas_pricing, Mapping):
        return {}

    nested_gas_price = gas_pricing.get("gasPrice")
    if isinstance(nested_gas_price, Mapping):
        if {
            "maxFeePerGas",
            "maxPriorityFeePerGas",
        }.issubset(nested_gas_price):
            return {
                "maxFeePerGas": int(nested_gas_price["maxFeePerGas"]),
                "maxPriorityFeePerGas": int(nested_gas_price["maxPriorityFeePerGas"]),
            }
        return {}

    max_fee_per_gas = gas_pricing.get("maxFeePerGas")
    max_priority_fee_per_gas = gas_pricing.get("maxPriorityFeePerGas")
    if max_fee_per_gas is not None or max_priority_fee_per_gas is not None:
        normalized = {}
        if max_fee_per_gas is not None:
            normalized["maxFeePerGas"] = int(max_fee_per_gas)
        if max_priority_fee_per_gas is not None:
            normalized["maxPriorityFeePerGas"] = int(max_priority_fee_per_gas)
        return normalized

    gas_price = gas_pricing.get("gasPrice")
    if gas_price is not None:
        return {"gasPrice": int(gas_price)}

    return {}


def _patch_multisend_encode_data() -> None:
    """
    Monkey-patch encode_data in the multisend contract to handle str data.

    web3.py 7.x returns str (hex-encoded) from encode_abi(), but the
    multisend contract's encode_data() expects bytes and uses cast(bytes, ...)
    which does not perform an actual conversion. This causes:
        TypeError: can't concat str to bytes

    The AEA / open-autonomy package manager deliberately replaces the
    ``packages`` module in ``sys.modules`` with a fake namespace module
    (``__path__ = None``), so we cannot rely on normal dotted-path imports.
    Instead we find the real module by scanning site-packages and load it
    directly via importlib.
    """
    import importlib.util
    import os
    import sys

    _ENCODE_MODULE = "packages.valory.contracts.multisend.contract"

    # If the module is already loaded into sys.modules and hasn't been broken
    # by the AEA package manager, use it directly.
    if _ENCODE_MODULE in sys.modules:
        _mod = sys.modules[_ENCODE_MODULE]
    else:
        # Find the physical location of the module on disk.
        site_packages_dirs = [
            p
            for p in sys.path
            if os.path.isdir(os.path.join(p, "packages"))
        ]
        if not site_packages_dirs:
            return  # cannot patch
        module_path = os.path.join(
            site_packages_dirs[0],
            "packages", "valory", "contracts", "multisend", "contract.py",
        )
        if not os.path.isfile(module_path):
            return  # cannot patch

        spec = importlib.util.spec_from_file_location(
            _ENCODE_MODULE,
            module_path,
        )
        if spec is None or spec.loader is None:
            return  # cannot patch
        _mod = importlib.util.module_from_spec(spec)
        sys.modules[_ENCODE_MODULE] = _mod
        spec.loader.exec_module(_mod)

    _orig = _mod.encode_data

    if getattr(_orig, "_patched", False):
        return  # already patched

    def _patched(tx):
        """Patched encode_data that handles both str and bytes data."""
        data = tx.get("data", b"")
        if isinstance(data, str):
            data = bytes.fromhex(data.removeprefix("0x"))
        tx_copy = dict(tx)
        tx_copy["data"] = data
        return _orig(tx_copy)

    _patched._patched = True  # type: ignore[attr-defined]
    _mod.encode_data = _patched


def _should_retry(error: str) -> bool:
    """Return whether the chain interaction should be retried."""
    if "Transaction with hash" in error and "not found" in error:
        return True
    return any(retryable in error for retryable in _RETRYABLE_CHAIN_ERRORS)


def _should_reprice(error: str) -> bool:
    """Return whether the tx should be repriced."""
    return "FeeTooLow" in error or "ReplacementNotAllowed" in error


def _should_rebuild(error: str) -> bool:
    """Return whether the tx should be rebuilt from scratch."""
    return any(
        nonce_error in error
        for nonce_error in ("wrong transaction nonce", "OldNonce", "nonce too low")
    )


def _normalize_tx_fee_fields(tx_dict: dict) -> dict:
    """Ensure the transaction uses valid legacy or EIP-1559 fee fields."""
    result = dict(tx_dict)
    nested_gas_price = result.get("gasPrice")
    if isinstance(nested_gas_price, Mapping):
        result.pop("gasPrice", None)
        result.update(_normalize_gas_pricing({"gasPrice": nested_gas_price}))
    if "maxFeePerGas" in result or "maxPriorityFeePerGas" in result:
        result.pop("gasPrice", None)
    return result


def _ensure_safe_tx_gas(ledger_api, tx_dict: dict) -> dict:
    """Replace unusable Safe tx gas with a real estimate or a sane fallback."""
    result = dict(tx_dict)
    current_gas = result.get("gas")
    if isinstance(current_gas, int) and current_gas > 21_000:
        return result

    estimate_tx = dict(result)
    estimate_tx.pop("gas", None)
    # web3.py 7.x estimate_gas may choke on nested dict values, so strip
    # any non-primitive values before passing to the RPC call.
    estimate_tx = {
        k: v
        for k, v in estimate_tx.items()
        if isinstance(v, (str, bytes, int, bool, type(None)))
    }
    try:
        result["gas"] = int(ledger_api.api.eth.estimate_gas(estimate_tx)) + 50_000
    except Exception:  # pylint: disable=broad-except
        result["gas"] = SAFE_TRANSFER_FALLBACK_GAS
    return result


def _transact_with_receipt(ledger_api, crypto, tx_builder) -> dict:
    """Build, sign, submit, and poll for a transaction receipt."""
    retries = 0
    tx_dict = None
    tx_digest = None
    already_known = False
    deadline = datetime.now().timestamp() + gnosis_utils.ON_CHAIN_INTERACT_TIMEOUT

    while (
        retries < gnosis_utils.ON_CHAIN_INTERACT_RETRIES
        and deadline >= datetime.now().timestamp()
    ):
        retries += 1
        try:
            if not already_known:
                tx_dict = tx_dict or tx_builder()
                if tx_dict is None:
                    raise ChainInteractionError("Got empty transaction")

                tx_signed = crypto.sign_transaction(transaction=tx_dict)
                tx_digest = ledger_api.send_signed_transaction(
                    tx_signed=tx_signed,
                    raise_on_try=True,
                )

            tx_receipt = ledger_api.api.eth.get_transaction_receipt(
                cast(str, tx_digest)
            )
            if tx_receipt is not None:
                return tx_receipt
        except RequestsConnectionError as e:
            raise RPCError("Cannot connect to the given RPC") from e
        except Exception as e:  # pylint: disable=broad-except
            error = str(e)
            if "Transaction with hash" in error and "not found" in error:
                already_known = True
                time.sleep(gnosis_utils.ON_CHAIN_INTERACT_SLEEP)
                continue
            if _should_reprice(error):
                tx_dict = _ensure_safe_tx_gas(
                    ledger_api,
                    _normalize_tx_fee_fields(tx_builder()),
                )
                continue
            if not _should_retry(error):
                error_lower = error.lower()
                if "revert" in error_lower:
                    raise ContractExecutionError(e) from e
                if "insufficient funds" in error_lower:
                    raise InsufficientFundsError(e) from e
                if "rate limit" in error_lower:
                    raise RateLimitError(e) from e
                raise ChainInteractionError(e) from e
            if _should_rebuild(error):
                tx_dict = None

            tx_digest = None
            already_known = False
            time.sleep(gnosis_utils.ON_CHAIN_INTERACT_SLEEP)

    raise ChainTimeoutError("Timed out when waiting for transaction to go through")


def transfer_erc20_from_safe_compat(
    ledger_api, crypto, safe: str, token: str, to: str, amount: float | int
) -> Optional[str]:
    """Transfer ERC20 assets from safe, normalizing malformed gas price fields."""
    amount = int(amount)
    instance = gnosis_utils.registry_contracts.erc20.get_instance(
        ledger_api=ledger_api,
        contract_address=token,
    )
    txd = instance.functions.transfer(to, amount)._encode_transaction_data()

    owner = ledger_api.api.to_checksum_address(crypto.address)

    def _build_tx(*args, **kwargs) -> dict:  # pylint: disable=unused-argument
        from packages.valory.contracts.gnosis_safe.contract import (  # type: ignore[import-untyped]
            GnosisSafeContract,
        )

        safe_contract = cast(
            GnosisSafeContract, gnosis_utils.registry_contracts.gnosis_safe
        )
        safe_tx_hash = safe_contract.get_raw_safe_transaction_hash(
            ledger_api=ledger_api,
            contract_address=safe,
            value=0,
            safe_tx_gas=0,
            to_address=token,
            data=bytes.fromhex(txd[2:]),
            operation=gnosis_utils.SafeOperation.CALL.value,
        ).get("tx_hash")
        safe_tx_bytes = binascii.unhexlify(safe_tx_hash[2:])
        signatures = {
            owner: crypto.sign_message(
                message=safe_tx_bytes,
                is_deprecated_mode=True,
            )[2:]
        }
        gas_pricing = _normalize_gas_pricing(ledger_api.try_get_gas_pricing())
        tx_dict = safe_contract.get_raw_safe_transaction(
            ledger_api=ledger_api,
            contract_address=safe,
            sender_address=owner,
            owners=(owner,),  # type: ignore
            to_address=token,
            value=0,
            data=bytes.fromhex(txd[2:]),
            safe_tx_gas=0,
            signatures_by_owner=signatures,
            operation=gnosis_utils.SafeOperation.CALL.value,
            nonce=ledger_api.api.eth.get_transaction_count(owner),
            gas_price=gas_pricing.get("gasPrice"),
            max_fee_per_gas=gas_pricing.get("maxFeePerGas"),
            max_priority_fee_per_gas=gas_pricing.get("maxPriorityFeePerGas"),
        )
        return _ensure_safe_tx_gas(
            ledger_api,
            _normalize_tx_fee_fields(tx_dict),
        )

    tx_receipt = _transact_with_receipt(
        ledger_api=ledger_api,
        crypto=crypto,
        tx_builder=_build_tx,
    )
    tx_hash = tx_receipt.get("transactionHash", "").hex()
    return tx_hash


class TritonService:
    """Trader"""

    def __init__(self, operate: OperateApp, service_config_id: str) -> None:
        """Constructor"""
        # Monkey-patch: encode_data in multisend contract must handle str data.
        # web3.py 7.x returns str from encode_abi(), but encode_data expects bytes.
        _patch_multisend_encode_data()

        self.service_manager = operate.service_manager()
        self.master_wallet = operate.wallet_manager.load(
            ledger_type=LedgerType.ETHEREUM
        )
        self.service = self.service_manager.load(service_config_id=service_config_id)
        self.logger = logging.getLogger(self.service.name)
        self.withdrawal_address = os.getenv("WITHDRAWAL_ADDRESS", None)

    @property
    def service_id(self) -> int:
        """Get the service id"""
        return self.service.chain_configs[self.service.home_chain].chain_data.token

    @property
    def agent_address(self) -> str:
        """Get the agent address"""
        if (
            len(
                self.service.chain_configs[self.service.home_chain].chain_data.instances
            )
            == 0
        ):
            raise ValueError("No agent instances found in the chain configuration")
        return self.service.chain_configs[self.service.home_chain].chain_data.instances[
            0
        ]

    @property
    def service_safe(self) -> str:
        """Get the service safe address"""
        return self.service.chain_configs[self.service.home_chain].chain_data.multisig

    # Fast path: when a service is staked, ownerOf(service_id) on the service
    # registry returns the staking contract address, and a single
    # getStakingState call confirms the service is bonded to it. This bypasses
    # service_manager._get_current_staking_program, which maps the owner
    # address back to a staking-program id by iterating the static STAKING
    # table and falls back to querying *every* known staking program (~47 RPC
    # calls, ~10s) when the contract isn't in that table. We only need the
    # address here, and get_staking_contract passes an unknown program id
    # (i.e. an address) straight through, so the program-id round trip is
    # unnecessary.
    @property
    def staking_contract_address(self) -> str:
        """Get the staking contract address (fast path)."""
        try:
            chain = self.service.home_chain
            chain_enum = Chain.from_string(chain)  # type: ignore[attr-defined]
            service_id = self.service_id
            if service_id == NON_EXISTENT_TOKEN:
                raise ValueError(
                    "Staking contract address not found: service has no on-chain token."
                )

            ledger_config = self.service.chain_configs[chain].ledger_config
            staking_manager = StakingManager(chain_enum, rpc=ledger_config.rpc)
            ledger_api = staking_manager.ledger_api

            service_registry = registry_contracts.service_registry.get_instance(
                ledger_api=ledger_api,
                contract_address=CONTRACTS[chain_enum]["service_registry"],
            )
            owner = service_registry.functions.ownerOf(service_id).call()

            # `owner` is the staking contract address when the service is
            # staked; otherwise it's the service owner and getStakingState
            # will either revert or return UNSTAKED.
            try:
                state = StakingState(
                    staking_manager.staking_ctr.get_instance(
                        ledger_api=ledger_api,
                        contract_address=owner,
                    )
                    .functions.getStakingState(service_id)
                    .call()
                )
            except (ChainInteractionError, RPCError, RequestsConnectionError, Web3ContractLogicError):
                # `owner` is not a staking contract → service is not staked.
                raise ValueError(
                    "Staking contract address not found: service is not staked."
                )

            if state == StakingState.UNSTAKED:
                raise ValueError(
                    "Staking contract address not found: service is not staked."
                )

            staking_contract_address = get_staking_contract(
                chain=chain,
                staking_program_id=owner,
            )
            if not staking_contract_address:
                raise ValueError(
                    f"Staking contract address not found for owner={owner}."
                )
            return staking_contract_address
        except ValueError:
            raise
        except KeyError as e:
            raise ValueError("Failed to get staking contract address.") from e

    def get_staking_status(self) -> dict:
        """Get the staking status"""
        self.logger.info("Checking staking status")
        try:
            staking_contract_address = self.staking_contract_address
            sftxb = self.service_manager.get_eth_safe_tx_builder(
                ledger_config=self.service.chain_configs[
                    self.service.home_chain
                ].ledger_config,
            )
            staking_params = sftxb.get_staking_params(
                staking_contract=staking_contract_address
            )
            activity_checker_contract_address = staking_params["activity_checker"]
        except KeyError as e:
            raise ValueError("Failed to get staking status.") from e

        try:
            requester_activity_checker = cast(
                RequesterActivityCheckerContract,
                RequesterActivityCheckerContract.from_dir(
                    directory=str(DATA_DIR / "contracts" / "requester_activity_checker")
                ),
            )
            mech = (
                requester_activity_checker.get_instance(
                    ledger_api=sftxb.ledger_api,
                    contract_address=activity_checker_contract_address,
                )
                .functions.mechMarketplace()
                .call()
            )
        except (ChainInteractionError, RPCError, RequestsConnectionError):
            try:
                mech_activity_contract = cast(
                    MechActivityContract,
                    MechActivityContract.from_dir(
                        directory=str(DATA_DIR / "contracts" / "mech_activity")
                    ),
                )
                mech = (
                    mech_activity_contract.get_instance(
                        ledger_api=sftxb.ledger_api,
                        contract_address=activity_checker_contract_address,
                    )
                    .functions.agentMech()
                    .call()
                )
            except (ChainInteractionError, RPCError, RequestsConnectionError):
                mech = "0x77af31De935740567Cf4fF1986D04B2c964A786a"

        return get_staking_status(
            mech_contract_address=mech,
            staking_token_address=staking_contract_address,
            activity_checker_address=activity_checker_contract_address,
            service_id=self.service_id,
            safe_address=self.service_safe,
        )

    def check_balance(self) -> dict:
        """Check the native balance"""
        chain_config = self.service.chain_configs[self.service.home_chain]
        if len(chain_config.chain_data.instances) == 0:
            raise ValueError("No agent instances found in the chain configuration")

        if self.master_wallet.safes is None:
            raise ValueError("Master wallet safes not found")

        agent_eoa_native_balance = get_native_balance(self.agent_address)
        service_safe_native_balance = get_native_balance(self.service_safe)
        service_safe_wrapped_native_balance = get_wrapped_native_balance(
            self.service_safe,
            Chain.from_string(self.service.home_chain),  # type: ignore[attr-defined]
        )
        master_eoa_native_balance = get_native_balance(
            self.master_wallet.crypto.address
        )
        master_safe_address = self.master_wallet.safes[
            Chain.from_string(self.service.home_chain)  # type: ignore[attr-defined]
        ]
        master_safe_native_balance = get_native_balance(master_safe_address)
        master_safe_olas_balance = get_olas_balance(master_safe_address) / 1e18
        service_safe_olas_balance = get_olas_balance(self.service_safe) / 1e18

        self.logger.info(
            "Agent EOA balance = %.2f xDAI "
            "| Service Safe balance: %.2f xDAI  %.2f wxDAI  %.2f OLAS "
            "| Master EOA balance: %.2f xDAI "
            "| Master Safe balance: %.2f xDAI",
            agent_eoa_native_balance,
            service_safe_native_balance,
            service_safe_wrapped_native_balance,
            service_safe_olas_balance,
            master_eoa_native_balance,
            master_safe_native_balance,
        )

        return {
            "agent_eoa_native_balance": agent_eoa_native_balance,
            "service_safe_native_balance": service_safe_native_balance,
            "service_safe_wrapped_native_balance": service_safe_wrapped_native_balance,
            "master_eoa_native_balance": master_eoa_native_balance,
            "master_safe_native_balance": master_safe_native_balance,
            "master_safe_olas_balance": master_safe_olas_balance,
            "service_safe_olas_balance": service_safe_olas_balance,
        }

    def claim_rewards(self) -> int:
        """Claim staking rewards"""

        self.logger.info("Claiming rewards")
        try:
            return self.service_manager.claim_on_chain_from_safe(
                service_config_id=self.service.service_config_id,
                chain=self.service.home_chain,
            )
        except (
            ChainInteractionError,
            ChainTimeoutError,
            RPCError,
            RequestsConnectionError,
        ):
            self.logger.error("Failed to claim rewards. %s", traceback.format_exc())

        return 0

    def withdraw_rewards(self) -> List[Tuple[Optional[str], float, str]]:
        """Withdraw staking rewards"""

        if not self.withdrawal_address:
            return []

        home_chain = Chain.from_string(self.service.home_chain)  # type: ignore[attr-defined]
        master_safe = self.master_wallet.safes[home_chain]

        try:
            master_safe_olas_balance = get_olas_balance(master_safe)
        except (
            ChainInteractionError,
            ChainTimeoutError,
            RPCError,
            RequestsConnectionError,
        ):
            self.logger.error("Failed to get OLAS balance. %s", traceback.format_exc())
            master_safe_olas_balance = 0

        withdrawals: List[Tuple[Optional[str], float, str]] = []
        if master_safe_olas_balance:
            self.logger.info(
                "Withdrawing %.2f OLAS rewards", master_safe_olas_balance / 1e18
            )
            olas_address = OLAS[home_chain]
            master_ledger_api = get_default_ledger_api(chain=home_chain)

            try:
                tx_hash = transfer_erc20_from_safe_compat(
                    ledger_api=master_ledger_api,
                    crypto=self.master_wallet.crypto,
                    safe=master_safe,
                    token=olas_address,
                    to=self.withdrawal_address,
                    amount=master_safe_olas_balance,
                )
                withdrawals.append(
                    (tx_hash, master_safe_olas_balance / 1e18, "Master Safe")
                )
            except (
                ChainInteractionError,
                ChainTimeoutError,
                RPCError,
                RequestsConnectionError,
            ):
                self.logger.error("Failed to withdraw OLAS. %s", traceback.format_exc())
        else:
            self.logger.info("No Master safe OLAS to withdraw")

        chain = Chain.from_string(self.service.home_chain)  # type: ignore[attr-defined]
        ledger_api = get_default_ledger_api(chain=chain)
        try:
            service_safe_olas_balance = get_olas_balance(self.service_safe) / 1e18
            if service_safe_olas_balance > 0:
                self.logger.info(
                    "Withdrawing %s OLAS from safe on %s to %s",
                    service_safe_olas_balance,
                    chain.value,
                    self.withdrawal_address,
                )
                ethereum_crypto = self.service_manager.keys_manager.get_crypto_instance(
                    self.service.agent_addresses[0]
                )
                tx_hash = transfer_erc20_from_safe_compat(
                    ledger_api=ledger_api,
                    crypto=ethereum_crypto,
                    safe=self.service_safe,
                    token=OLAS[chain],
                    to=self.withdrawal_address,
                    amount=service_safe_olas_balance * 1e18,
                )
                withdrawals.append((tx_hash, service_safe_olas_balance, "Service Safe"))
        except (
            ChainInteractionError,
            ChainTimeoutError,
            RPCError,
            RequestsConnectionError,
        ):
            self.logger.error(
                "Failed to withdraw OLAS from service safe. %s", traceback.format_exc()
            )

        return withdrawals
