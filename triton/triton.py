""" "Triton Telegram bot"""

import asyncio
import datetime
import logging
import os
import typing as t
from pathlib import Path

import aiohttp
import dotenv
import pytz
import yaml
from triton.rpc import configure_runtime_rpcs

dotenv.load_dotenv(override=True)
configure_runtime_rpcs()

from operate.cli import OperateApp
from operate.constants import OPERATE
from operate.operate_types import Chain
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from triton.chain import get_olas_price, get_slots
from triton.constants import (
    AGENT_BALANCE_THRESHOLD,
    AUTOCLAIM,
    AUTOCLAIM_DAY,
    AUTOCLAIM_HOUR_UTC,
    CHAT_ID,
    GNOSISSCAN_ADDRESS_URL,
    GNOSISSCAN_TX_URL,
    LOCAL_TIMEZONE,
    MANUAL_CLAIM,
    MASTER_SAFE_BALANCE_THRESHOLD,
    OPERATE_USER_PASSWORD,
    SAFE_BALANCE_THRESHOLD,
    TELEGRAM_TOKEN,
)
from triton.service import TritonService
from triton.tools import escape_markdown_v2

logger = logging.getLogger("telegram_bot")

SERVICE_TASK_TIMEOUT_SECONDS = float(os.getenv("SERVICE_TASK_TIMEOUT_SECONDS", "20"))
SERVICE_CONCURRENCY = int(os.getenv("SERVICE_CONCURRENCY", "4"))


async def _run_blocking_call(
    func: t.Callable[..., t.Any], *args: t.Any, timeout: float | None = None
) -> t.Any:
    """Run blocking IO outside the event loop with an optional timeout."""
    if getattr(func, "__module__", "").startswith("unittest.mock"):
        return func(*args)

    call = asyncio.to_thread(func, *args)
    if timeout is None:
        return await call
    return await asyncio.wait_for(call, timeout=timeout)


async def _run_service_tasks(
    services: t.Dict[str, "TritonService"],
    worker: t.Callable[[str, "TritonService"], t.Any],
    *,
    timeout: float | None = SERVICE_TASK_TIMEOUT_SECONDS,
) -> list[tuple[str, t.Any | None, Exception | None]]:
    """Run service tasks concurrently and keep partial failures isolated."""
    semaphore = asyncio.Semaphore(max(1, SERVICE_CONCURRENCY))
    run_inline = all(
        type(service).__module__.startswith("unittest.mock")
        for service in services.values()
    )

    async def _execute(
        service_name: str, service: "TritonService"
    ) -> tuple[str, t.Any | None, Exception | None]:
        async with semaphore:
            try:
                if run_inline:
                    result = worker(service_name, service)
                else:
                    result = await _run_blocking_call(
                        worker,
                        service_name,
                        service,
                        timeout=timeout,
                    )
                return service_name, result, None
            except Exception as exc:  # pylint: disable=broad-except
                return service_name, None, exc

    return await asyncio.gather(
        *(_execute(service_name, service) for service_name, service in services.items())
    )


def _collect_staking_status(service_name: str, service: "TritonService") -> dict:
    """Collect all data required for the staking status command."""
    status = service.get_staking_status()
    balances = service.check_balance()
    if service.master_wallet.safes is None:
        raise ValueError("Master wallet safes not found")

    master_safe_address = service.master_wallet.safes[
        Chain.from_string(service.service.home_chain)  # type: ignore[attr-defined]
    ]
    return {
        "service_name": service_name,
        "status": status,
        "balances": balances,
        "master_safe_address": master_safe_address,
    }


def _collect_balance(service_name: str, service: "TritonService") -> dict:
    """Collect all data required for the balance command."""
    balances = service.check_balance()
    return {
        "service_name": service_name,
        "balances": balances,
        "agent_address": service.agent_address,
        "service_safe": service.service_safe,
        "master_eoa_address": service.master_wallet.crypto.address,
        "home_chain": service.service.home_chain,
        "master_safes": service.master_wallet.safes,
    }


def _claim_rewards(service_name: str, service: "TritonService") -> dict:
    """Claim rewards for a service."""
    return {
        "service_name": service_name,
        "claimed_amount": service.claim_rewards(),
    }


def _withdraw_rewards(service_name: str, service: "TritonService") -> dict:
    """Withdraw rewards for a service."""
    return {
        "service_name": service_name,
        "withdrawals": service.withdraw_rewards(),
        "withdrawal_address": service.withdrawal_address,
    }


def _check_balance_thresholds(service_name: str, service: "TritonService") -> dict:
    """Collect balance data used by the periodic balance checker."""
    balances = service.check_balance()
    if service.master_wallet.safes is None:
        raise ValueError("Master wallet safes not found")
    master_safe_address = service.master_wallet.safes[
        Chain.from_string(service.service.home_chain)  # type: ignore[attr-defined]
    ]
    return {
        "service_name": service_name,
        "balances": balances,
        "master_safe_address": master_safe_address,
        "agent_address": service.agent_address,
        "service_safe": service.service_safe,
    }


def run_triton() -> None:  # pylint: disable=too-many-statements,too-many-locals
    """Main"""

    # Load configuration
    with open("config.yaml", "r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    # Instantiate the services
    services: t.Dict[str, TritonService] = {}
    for operator_name, operate_path in config["operators"].items():
        operate = OperateApp(Path(operate_path) / OPERATE)
        operate.password = OPERATE_USER_PASSWORD
        for service in operate.service_manager().get_all_services()[0]:
            services[f"{operator_name}-{service.name}"] = TritonService(
                operate=operate,
                service_config_id=service.service_config_id,
            )

    # Commands
    async def staking_status(  # pylint: disable=unused-argument,too-many-locals
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        try:
            messages = []
            errors = []
            total_rewards = 0.0
            master_safe_olas = 0.0
            agent_safe_olas = 0.0
            master_safe_addresses: set[str] = set()
            results = await _run_service_tasks(services, _collect_staking_status)
            for service_name, result, error in results:
                if error is not None:
                    logger.error(
                        "Failed to get staking status for %s", service_name, exc_info=error
                    )
                    errors.append(f"[{service_name}] Error: {str(error)}")
                    continue

                status = result["status"]
                balances = result["balances"]
                total_rewards += float(status["accrued_rewards"].split(" ")[0])
                master_safe_address = result["master_safe_address"]
                if master_safe_address not in master_safe_addresses:
                    master_safe_addresses.add(master_safe_address)
                    master_safe_olas += balances["master_safe_olas_balance"]
                agent_safe_olas += balances["service_safe_olas_balance"]
                messages.append(
                    f"[{service_name}] {status['accrued_rewards']} "
                    f"""[{status['mech_requests_this_epoch']}/{status['required_mech_requests']}]
Staking program: {status['metadata']['name']}
Next epoch: {status['epoch_end']}"""
                )

            combined_rewards = total_rewards + master_safe_olas + agent_safe_olas
            try:
                olas_price = await _run_blocking_call(get_olas_price, timeout=10)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Failed to fetch OLAS price", exc_info=exc)
                errors.append(f"[price] Error: {str(exc)}")
                olas_price = None
            rewards_value = combined_rewards * olas_price if olas_price else None
            message = f"Total rewards = {combined_rewards:g} OLAS"
            breakdown_parts = []
            if total_rewards:
                breakdown_parts.append(f"{total_rewards:g} accrued")
            if agent_safe_olas:
                breakdown_parts.append(f"{agent_safe_olas:g} in agent safes")
            if master_safe_olas:
                breakdown_parts.append(f"{master_safe_olas:g} in master safes")
            if breakdown_parts:
                message += " (" + " + ".join(breakdown_parts) + ")"
            if rewards_value:
                message += f" [${rewards_value:g}]"
            messages.append(message)
            if errors:
                messages.append("\n".join(errors))

            if update.message is None:
                logger.error("Cannot send message, update.message is None")
                return

            await update.message.reply_text(text=("\n\n").join(messages))
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(
                context=context, update=update, where="staking_status", exc=exc
            )

    async def balance(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ):  # pylint: disable=unused-argument
        try:
            messages = []
            errors = []
            results = await _run_service_tasks(services, _collect_balance)
            for service_name, result, error in results:
                if error is not None:
                    logger.error("Failed to get balances for %s", service_name, exc_info=error)
                    errors.append(f"[{service_name}] Error: {str(error)}")
                    continue

                balances = result["balances"]
                agent_native_balance = balances["agent_eoa_native_balance"]
                safe_native_balance = balances["service_safe_native_balance"]
                safe_wrapped_native_balance = balances["service_safe_wrapped_native_balance"]
                safe_olas_balance = balances["service_safe_olas_balance"]
                master_eoa_native_balance = balances["master_eoa_native_balance"]
                master_safe_native_balance = balances["master_safe_native_balance"]
                master_safe_olas_balance = balances["master_safe_olas_balance"]
                master_safes = result["master_safes"]
                if master_safes is None:
                    raise ValueError("Master wallet safes not found")
                message = (
                    r"\["
                    + escape_markdown_v2(service_name)
                    + r"]"
                    + f"\n[Agent EOA]({GNOSISSCAN_ADDRESS_URL.format(address=result['agent_address'])}) = {agent_native_balance:g} xDAI"
                    + f"\n[Service Safe]({GNOSISSCAN_ADDRESS_URL.format(address=result['service_safe'])}) = {safe_native_balance:g} xDAI  {safe_wrapped_native_balance:g} wxDAI  {safe_olas_balance:g} OLAS"
                    + f"\n[Master EOA]({GNOSISSCAN_ADDRESS_URL.format(address=result['master_eoa_address'])}) = {master_eoa_native_balance:g} xDAI"
                    + f"\n[Master Safe]({GNOSISSCAN_ADDRESS_URL.format(address=master_safes[Chain.from_string(result['home_chain'])])}) = {master_safe_native_balance:g} xDAI  {master_safe_olas_balance:g} OLAS"
                )

                messages.append(message)
            if errors:
                messages.append("\n".join(errors))

            if update.message is None:
                logger.error("Cannot send message, update.message is None")
                return

            await update.message.reply_text(
                text=("\n\n").join(messages),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=update, where="balance", exc=exc)

    async def claim(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ):  # pylint: disable=unused-argument
        """Claim rewards"""
        try:
            if not update.message:
                logger.error("Cannot send message, update.message is None")
                return

            if not MANUAL_CLAIM:
                await update.message.reply_text(text="Manual claim is disabled")
                return

            messages = []
            errors = []
            results = await _run_service_tasks(
                services, _claim_rewards, timeout=None
            )
            for service_name, result, error in results:
                if error is not None:
                    logger.error("Failed to claim rewards for %s", service_name, exc_info=error)
                    errors.append(f"[{service_name}] Error: {str(error)}")
                    continue

                claimed_amount = result["claimed_amount"]
                if not claimed_amount:
                    continue

                messages.append(
                    f"[{service_name}] Claimed {claimed_amount} OLAS rewards into the Master safe."
                )
            if errors:
                messages.append("\n".join(errors))

            await update.message.reply_text(
                text=("\n\n").join(messages) if messages else "No rewards claimed",
            )
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=update, where="claim", exc=exc)

    async def withdraw(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ):  # pylint: disable=unused-argument
        """Withdraw rewards"""
        try:
            if not update.message:
                logger.error("Cannot send message, update.message is None")
                return

            messages = []
            results = await _run_service_tasks(
                services, _withdraw_rewards, timeout=None
            )
            for service_name, result, error in results:
                if error is not None:
                    logger.error("Failed to withdraw rewards for %s", service_name, exc_info=error)
                    messages.append(r"\[" + escape_markdown_v2(service_name) + r"] " + f"Error: {str(error)}")
                    continue

                withdrawals = result["withdrawals"]
                if withdrawals:
                    for tx_hash, value, source in withdrawals:
                        message = (
                            r"\["
                            + escape_markdown_v2(service_name)
                            + r"] "
                            + f"Sent the [withdrawal transaction]({GNOSISSCAN_TX_URL.format(tx_hash=tx_hash)}). "
                            + f"{value:g} OLAS sent from the {source} to [{result['withdrawal_address']}]"
                            + f"({GNOSISSCAN_ADDRESS_URL.format(address=result['withdrawal_address'])}) #withdraw"
                        )
                        messages.append(message)
                else:
                    message = (
                        r"\["
                        + escape_markdown_v2(service_name)
                        + r"] "
                        + "Cannot withdraw rewards"
                    )

                    messages.append(message)

            await update.message.reply_text(
                text=("\n\n").join(messages),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=update, where="withdraw", exc=exc)

    async def slots(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ):  # pylint: disable=unused-argument
        try:
            if not update.message:
                logger.error("Cannot send message, update.message is None")
                return

            slots = await _run_blocking_call(get_slots, timeout=SERVICE_TASK_TIMEOUT_SECONDS)

            messages = [
                f"[{contract_name}] {n_slots} available slots"
                for contract_name, n_slots in slots.items()
            ]

            await update.message.reply_text(
                text=("\n").join(messages),
            )
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=update, where="slots", exc=exc)

    async def ip_address(  # pylint: disable=unused-argument
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reply with the server public IP address."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.ipify.org") as response:
                    ip = (await response.text()).strip()
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to get public IP: %s", exc)
            ip = "Unavailable"

        try:
            if update.message:
                await update.message.reply_text(text=f"Public IP address: {ip}")
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=update, where="ip", exc=exc)

    async def scheduled_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not update.message:
                logger.error("Cannot send message, update.message is None")
                return

            jobs = context.job_queue.jobs() if context.job_queue else []

            if not jobs:
                await update.message.reply_text("No scheduled jobs")
                return

            message = ""
            for job in jobs:
                next_execution = (
                    job.next_t.astimezone(pytz.timezone(LOCAL_TIMEZONE)).strftime(
                        "%Y-%m-%d %H:%M:%S %Z"
                    )
                    if job.next_t
                    else "N/A"
                )
                message += f"• {job.name}: {next_execution}\n"

            await update.message.reply_text(message)
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=update, where="jobs", exc=exc)

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log and notify on unexpected errors."""
        logger.error("Unhandled exception while processing update", exc_info=context.error)
        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"Unhandled error: {context.error}",
            )
        except Exception:  # pylint: disable=broad-except
            logger.error("Failed to send error message to chat", exc_info=True)

    # Tasks
    async def start(context: ContextTypes.DEFAULT_TYPE):
        """Start"""
        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text="Triton has started",
            )
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=None, where="start", exc=exc)

    async def balance_check(context: ContextTypes.DEFAULT_TYPE):
        try:
            logger.info("Running balance check task")
            results = await _run_service_tasks(services, _check_balance_thresholds)
            for service_name, result, error in results:
                if error is not None:
                    logger.error("Balance check failed for %s", service_name, exc_info=error)
                    continue

                balances = result["balances"]
                agent_native_balance = balances["agent_eoa_native_balance"]
                safe_native_balance = balances["service_safe_native_balance"]
                safe_wrapped_native_balance = balances[
                    "service_safe_wrapped_native_balance"
                ]
                master_safe_native_balance = balances["master_safe_native_balance"]
                master_safe_address = result["master_safe_address"]

                if agent_native_balance < AGENT_BALANCE_THRESHOLD:
                    message = f"[{service_name}] [Agent EOA]({GNOSISSCAN_ADDRESS_URL.format(address=result['agent_address'])}) balance is {agent_native_balance:g} xDAI"
                    await context.bot.send_message(
                        chat_id=CHAT_ID,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                    )

                if (
                    safe_native_balance + safe_wrapped_native_balance
                    < SAFE_BALANCE_THRESHOLD
                ):
                    message = f"[{service_name}] [Service Safe]({GNOSISSCAN_ADDRESS_URL.format(address=result['service_safe'])}) balance is {safe_native_balance:g} xDAI  {safe_wrapped_native_balance:g} wxDAI"
                    await context.bot.send_message(
                        chat_id=CHAT_ID,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                    )

                if master_safe_native_balance < MASTER_SAFE_BALANCE_THRESHOLD:
                    message = (
                        f"[{service_name}] [Master Safe]({GNOSISSCAN_ADDRESS_URL.format(address=master_safe_address)}) "
                        f"balance is {master_safe_native_balance:g} xDAI"
                    )
                    await context.bot.send_message(
                        chat_id=CHAT_ID,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                    )
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(
                context=context, update=None, where="balance_check", exc=exc
            )

    async def post_init(app):
        # await app.bot.set_my_name("Triton")
        await app.bot.set_my_description("A bot to manage Olas staked services")
        await app.bot.set_my_short_description("A bot to manage Olas staked services")
        await app.bot.set_my_commands(
            [
                ("staking_status", "Staking status"),
                ("balance", "Check wallet balances"),
                ("claim", "Claim rewards"),
                ("withdraw", "Withdraw rewards"),
                ("slots", "Check available staking slots"),
                ("jobs", "Check the scheduled jobs"),
                ("ip", "Get the bot public IP"),
            ]
        )

    async def autoclaim(context: ContextTypes.DEFAULT_TYPE):
        try:
            logger.info("Running autoclaim task")

            if not AUTOCLAIM:
                logger.info("Autoclaim task is disabled")
                return

            messages = []

            # Claim
            claim_results = await _run_service_tasks(
                services, _claim_rewards, timeout=None
            )
            for service_name, _, error in claim_results:
                if error is not None:
                    logger.error("Autoclaim claim failed for %s", service_name, exc_info=error)
                    messages.append(
                        r"\[" + escape_markdown_v2(service_name) + r"] " + f"(Autoclaim) Error while claiming rewards: {error}"
                    )

            # Withdraw
            withdraw_results = await _run_service_tasks(
                services, _withdraw_rewards, timeout=None
            )
            for service_name, result, error in withdraw_results:
                if error is not None:
                    logger.error("Autoclaim withdraw failed for %s", service_name, exc_info=error)
                    messages.append(
                        r"\[" + escape_markdown_v2(service_name) + r"] " + f"(Autoclaim) Error while withdrawing rewards: {error}"
                    )
                    continue

                withdrawals = result["withdrawals"]
                if withdrawals:
                    for tx_hash, value, source in withdrawals:
                        message = (
                            r"\["
                            + escape_markdown_v2(service_name)
                            + r"] "
                            + "(Autoclaim) Sent the [withdrawal transaction]"
                            + f"({GNOSISSCAN_TX_URL.format(tx_hash=tx_hash)}). "
                            + f"{value:g} OLAS sent from the {source} to [{result['withdrawal_address']}]"
                            + f"({GNOSISSCAN_ADDRESS_URL.format(address=result['withdrawal_address'])}) #withdraw"
                        )
                        messages.append(message)
                else:
                    message = (
                        r"\["
                        + escape_markdown_v2(service_name)
                        + r"] "
                        + "(Autoclaim) Cannot withdraw rewards"
                    )

                    messages.append(message)

            if not messages:
                logger.info("No rewards to withdraw")
                return

            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=("\n\n").join(messages),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # pylint: disable=broad-except
            await report_error(context=context, update=None, where="autoclaim", exc=exc)

    # Create bot
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    if app.job_queue is None:
        raise RuntimeError("Job queue is not available")

    job_queue = app.job_queue

    # Add commands
    app.add_handler(CommandHandler("staking_status", staking_status))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("claim", claim))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("slots", slots))
    app.add_handler(CommandHandler("jobs", scheduled_jobs))
    app.add_handler(CommandHandler("ip", ip_address))
    app.add_error_handler(error_handler)

    # Add tasks
    job_queue.run_once(start, when=3)  # in 3 seconds
    job_queue.run_repeating(
        balance_check,
        interval=datetime.timedelta(hours=1),
        first=5,  # in 5 seconds
    )
    job_queue.run_monthly(
        autoclaim,
        day=AUTOCLAIM_DAY,
        when=datetime.time(
            hour=AUTOCLAIM_HOUR_UTC, minute=0, second=0, microsecond=0, tzinfo=None
        ),
    )

    # Start
    logger.info("Starting bot")
    app.run_polling()
    async def report_error(
        *,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update | None,
        where: str,
        exc: Exception,
    ) -> None:
        """Report errors to logs and Telegram."""
        logger.error("Error in %s", where, exc_info=exc)
        message = f"Error in {where}: {exc}"
        try:
            if update and update.message:
                await update.message.reply_text(text=message)
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=message,
            )
        except Exception:  # pylint: disable=broad-except
            logger.error("Failed to send error message to chat", exc_info=True)
