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

import dotenv
from autonomy.chain.exceptions import ChainInteractionError, ChainTimeoutError, RPCError
from requests.exceptions import ConnectionError as RequestsConnectionError
from triton.rpc import configure_runtime_rpcs

dotenv.load_dotenv(override=True)
configure_runtime_rpcs()

from operate.cli import OperateApp
from operate.data import DATA_DIR
from operate.data.contracts.mech_activity.contract import MechActivityContract
from operate.data.contracts.requester_activity_checker.contract import (
    RequesterActivityCheckerContract,
)
from operate.ledger import get_default_ledger_api
from operate.ledger.profiles import OLAS, get_staking_contract
from operate.operate_types import Chain, LedgerType
from operate.utils import gnosis as gnosis_utils

from triton.chain import (
    get_native_balance,
    get_olas_balance,
    get_staking_status,
    get_wrapped_native_balance,
)

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
    nested_gas_price = tx_dict.get("gasPrice")
    if isinstance(nested_gas_price, Mapping):
        tx_dict.pop("gasPrice", None)
        tx_dict.update(_normalize_gas_pricing({"gasPrice": nested_gas_price}))
    if "maxFeePerGas" in tx_dict or "maxPriorityFeePerGas" in tx_dict:
        tx_dict.pop("gasPrice", None)
    return tx_dict


def _ensure_safe_tx_gas(ledger_api, tx_dict: dict) -> dict:
    """Replace unusable Safe tx gas with a real estimate or a sane fallback."""
    current_gas = tx_dict.get("gas")
    if isinstance(current_gas, int) and current_gas > 21_000:
        return tx_dict

    estimate_tx = dict(tx_dict)
    estimate_tx.pop("gas", None)
    try:
        tx_dict["gas"] = int(ledger_api.api.eth.estimate_gas(estimate_tx)) + 50_000
    except Exception:  # pylint: disable=broad-except
        tx_dict["gas"] = SAFE_TRANSFER_FALLBACK_GAS
    return tx_dict


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

            tx_receipt = ledger_api.api.eth.get_transaction_receipt(cast(str, tx_digest))
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
                raise ChainInteractionError(error) from e
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
        safe_tx_hash = gnosis_utils.registry_contracts.gnosis_safe.get_raw_safe_transaction_hash(
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
        tx_dict = gnosis_utils.registry_contracts.gnosis_safe.get_raw_safe_transaction(
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

    @property
    def staking_contract_address(self) -> str:
        """Get the staking contract address"""
        try:
            current_staking_program = self.service_manager._get_current_staking_program(  # pylint: disable=protected-access  # noqa: E501
                service=self.service, chain=self.service.home_chain
            )
            staking_contract_address = get_staking_contract(
                chain=self.service.home_chain,
                staking_program_id=current_staking_program,
            )
            if not staking_contract_address:
                raise ValueError(
                    f"Staking contract address not found for {current_staking_program=}."
                )

            return staking_contract_address
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
        except Exception:  # pylint: disable=broad-except
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
            except Exception:  # pylint: disable=broad-except
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
        except Exception:  # pylint: disable=broad-except
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
        except Exception:  # pylint: disable=broad-except
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
            except Exception:  # pylint: disable=broad-except
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
        except Exception:  # pylint: disable=broad-except
            self.logger.error(
                "Failed to withdraw OLAS from service safe. %s", traceback.format_exc()
            )

        return withdrawals
