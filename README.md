
> [!CAUTION]
> ## Disclaimer
>
> This project is provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement.
>
> The authors or contributors shall not be held liable for any claim, damages, or other liabilities arising from the use of this software, whether in an action of contract, tort, or otherwise.
>
> Use this software at your own risk. Always verify its applicability and security for your specific use case before deployment.
>

# Triton

> [!NOTE]
> *Triton, a figure from Greek mythology and son of Poseidon, had the ability to calm and stirr the waves by blowing his conch shell.*

Triton is a Telegram bot to handle staked Olas services on the Gnosis chain. Triton can help you to:
- Monitor your wallet balances (agent, safe and operator wallets) and receive an alert when they are too low.
- Check your staking status (mech requests and rewards)
- Check empty slots on staking contracts
- Claim your rewards (manual and automatic mode)
- Withdraw your OLAS
- **Automatic hourly balance monitoring** — sends a Telegram alert when any wallet balance drops below the configured thresholds
- **Automatic monthly claiming** — optional autoclaim that claims rewards and withdraws them to your address on a configurable day/time
- **Startup notification** — sends a message when the bot starts
- **Error reporting** — unhandled errors are sent to Telegram

Point triton to all your trader_quickstart folder locations (they have to contain the `.operate` folder) and it will handle them.

## Architecture

```
triton/
├── __init__.py
├── chain.py       # Blockchain interactions (balances, contracts, slots, OLAS price)
├── constants.py   # Constants and env var loading
├── exceptions.py  # Custom exception hierarchy (InsufficientFundsError, ContractExecutionError, RateLimitError)
├── rpc.py         # Runtime RPC configuration
├── service.py     # TritonService class (staking, balance, claim, withdraw)
├── tools.py       # Utility functions (markdown escaping, number formatting, type conversion)
└── triton.py      # Bot commands, periodic jobs, service orchestration
run.py             # Entry point
```

</br>
<p align="center">
  <img width="50%" src="images/triton.jpg">
</p>

## Requirements

- Python >=3.10, <3.12
- [Poetry](https://python-poetry.org/) for dependency management

## Prepare the repo

1. Clone the repo:

    ```bash
    git clone https://github.com/dagacha/triton-bot.git
    cd triton-bot
    ```

2. Prepare the virtual environment:

    ```bash
    poetry shell
    poetry install
    ```

3. Copy the env file:

    ```bash
    cp sample.env .env
    ```

    And fill in the required environment variables (see [Configuration](#configuration)).

4. Edit `config.yaml` and add the path to your trader_quickstart folders. Multiple instances can be added.

    The `config.yaml` should follow this structure:

    ```yaml
    operators:
      name1: /path/to/trader1
      name2: /path/to/trader2
    ```

## Configuration

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GNOSIS_RPC` | Yes | — | Gnosis RPC endpoint |
| `TELEGRAM_TOKEN` | Yes | — | Telegram bot API token (get one from @BotFather) |
| `CHAT_ID` | Yes | — | Telegram chat ID for alerts and notifications |
| `OPERATE_USER_PASSWORD` | Yes | — | Password of the operator user account |
| `COINGECKO_API_KEY` | No | — | CoinGecko API key for checking reward USD value |
| `WITHDRAWAL_ADDRESS` | No | — | Address to send withdrawn rewards to |
| `AGENT_BALANCE_THRESHOLD` | No | `0.1` | Agent EOA balance threshold (xDAI) |
| `SAFE_BALANCE_THRESHOLD` | No | `1` | Service Safe balance threshold (xDAI + wxDAI) |
| `MASTER_SAFE_BALANCE_THRESHOLD` | No | `5` | Master Safe balance threshold (xDAI) |
| `MANUAL_CLAIM` | No | `true` | Enable the `/claim` command |
| `AUTOCLAIM` | No | `false` | Enable automatic monthly claiming |
| `AUTOCLAIM_DAY` | No | `1` | Day of month for autoclaim |
| `AUTOCLAIM_HOUR_UTC` | No | `9` | UTC hour for autoclaim |
| `LOCAL_TIMEZONE` | No | `UTC` | Timezone for displayed timestamps (e.g. `Europe/Madrid`) |
| `RPC_TIMEOUT_SECONDS` | No | `5` | Web3 RPC call timeout |
| `HTTP_CONNECT_TIMEOUT_SECONDS` | No | `5` | HTTP connect timeout for external API calls |
| `HTTP_READ_TIMEOUT_SECONDS` | No | `10` | HTTP read timeout for external API calls |
| `PRICE_CACHE_TTL_SECONDS` | No | `300` | OLAS price cache TTL |
| `METADATA_CACHE_TTL_SECONDS` | No | `3600` | Staking metadata cache TTL |
| `SAFE_TRANSFER_FALLBACK_GAS` | No | `500000` | Fallback gas limit for Safe ERC20 transfers |
| `SERVICE_TASK_TIMEOUT_SECONDS` | No | `20` | Timeout per service task (staking status, balance check) |
| `SERVICE_CONCURRENCY` | No | `4` | Max concurrent service operations |

### Operator configuration (`config.yaml`)

The `config.yaml` file maps operator names to their quickstart directory paths. Each directory must contain a `.operate` folder:

```yaml
operators:
  trader13: /path/to/trader13/quickstart
  trader14: /path/to/trader14/quickstart
```

## Run Triton as a python script

```bash
poetry run python run.py
```

## Run Triton as a systemd service

1. Install:

    ```bash
    make install
    ```

2. Start the service:

    ```bash
    make start
    ```

3. Verify it is working:
    ```bash
    systemctl status triton.service
    ```

## Telegram bot commands

| Command | Description |
|---------|-------------|
| `/staking_status` | Check staking status, accrued rewards, and mech request progress for all services |
| `/balance` | Check wallet balances (Agent EOA, Service Safe, Master EOA, Master Safe) |
| `/claim` | Manually claim accrued staking rewards into the Master Safe |
| `/withdraw` | Withdraw OLAS from Master/Service Safes to the configured withdrawal address |
| `/slots` | Check available slots on all staking contracts |
| `/jobs` | List the currently scheduled periodic jobs |
| `/ip` | Get the bot server's public IP address |
| `/run <id>` | Run `run_service_cron.sh` for a trader (e.g. `/run 21` or `/run trader21`) |
| `/stop <id>` | Run `stop_service_cron.sh` for a trader |
| `/run_all` | Run `run_all_traders.sh` from the user's home directory |
| `/stop_all` | Run `stop_all_traders.sh` from the user's home directory |

## Periodic jobs

| Job | Interval | Description |
|-----|----------|-------------|
| `start` | Once, 3s after boot | Sends a "Triton has started" notification |
| `balance_check` | Every hour | Checks all wallet balances against configured thresholds; sends alerts for any below-threshold wallets |
| `autoclaim` | Monthly (configurable day/hour) | Claims staking rewards and withdraws OLAS to the configured address |

## Staking contracts

Triton monitors the following staking contract tiers on Gnosis chain:

| Name | Stake | Address | Slots |
|------|-------|---------|-------|
| Hobbyist (100 OLAS) | 100 OLAS | `0x389b46c259631acd6a69bde8b6cee218230bae8c` | 100 |
| Hobbyist 2 (500 OLAS) | 500 OLAS | `0x238eb6993b90a978ec6aad7530d6429c949c08da` | 50 |
| Expert (1k OLAS) | 1,000 OLAS | `0x5344b7dd311e5d3dddd46a4f71481bd7b05aaa3e` | 20 |
| Expert 2 (1k OLAS) | 1,000 OLAS | `0xb964e44c126410df341ae04b13ab10a985fe3513` | 40 |
| Expert 3 (2k OLAS) | 2,000 OLAS | `0x80fad33cadb5f53f9d29f02db97d682e8b101618` | 20 |
| Expert 4 (10k OLAS) | 10,000 OLAS | `0xad9d891134443b443d7f30013c7e14fe27f2e029` | 26 |
| Expert 5 (10k OLAS) | 10,000 OLAS | `0xe56df1e563de1b10715cb313d514af350d207212` | 26 |
| Expert 6 (1k OLAS) | 1,000 OLAS | `0x2546214aee7eea4bee7689c81231017ca231dc93` | 40 |
| Expert 7 (10k OLAS) | 10,000 OLAS | `0xd7a3c8b975f71030135f1a66e9e23164d54ff455` | 26 |

## Internal architecture

### Transaction execution and retry logic

`_transact_with_receipt` (`triton/service.py`) handles Safe transaction submission with automatic retry:

- **Reprice** — if `FeeTooLow` or `ReplacementNotAllowed` is detected, the transaction is rebuilt with updated gas pricing
- **Rebuild** — if a nonce error (`wrong transaction nonce`, `OldNonce`, `nonce too low`) occurs, the transaction is rebuilt from scratch with a fresh nonce
- **Retry** — if the receipt is not yet available ("Transaction with hash ... not found"), polling continues with a sleep interval
- **Non-retryable errors** are mapped to typed exceptions:
  - `ContractExecutionError` — contract reverts
  - `InsufficientFundsError` — insufficient funds for gas
  - `RateLimitError` — RPC rate limiting
  - `ChainInteractionError` — all other chain errors
- **Timeout** — the loop respects `ON_CHAIN_INTERACT_RETRIES` and `ON_CHAIN_INTERACT_TIMEOUT` from `gnosis_utils`

### Gas pricing normalization

`_normalize_gas_pricing` and `_normalize_tx_fee_fields` handle malformed gas pricing from the ledger API:

- Nested `gasPrice` objects (containing `maxFeePerGas`/`maxPriorityFeePerGas`) are flattened to top-level EIP-1559 fields
- Mixed fee inputs (legacy `gasPrice` + EIP-1559 fields) are resolved to EIP-1559 only
- `_ensure_safe_tx_gas` replaces unusable gas values (≤ 21,000) with an on-chain estimate + 50,000 buffer, falling back to `SAFE_TRANSFER_FALLBACK_GAS`

### Safe ERC20 transfer

`transfer_erc20_from_safe_compat` handles ERC20 transfers from a Gnosis Safe:

1. Encodes the ERC20 `transfer` call data
2. Builds a Safe transaction hash and collects the owner's signature
3. Constructs the raw Safe transaction with normalized gas pricing
4. Submits via `_transact_with_receipt` with full retry logic

### Staking contract resolution (fast path)

`TritonService.staking_contract_address` resolves the staking contract without iterating all known staking programs:

1. Calls `ownerOf(service_id)` on the Service Registry — when staked, the owner is the staking contract address
2. Verifies the service is in a `STAKED` state via `getStakingState`
3. Passes the address through `get_staking_contract` to get the canonical staking program address

This avoids ~47 RPC calls that the standard `_get_current_staking_program` path would make.

### Mech address resolution

`TritonService.get_staking_status` resolves the mech marketplace contract address through a fallback chain:

1. **RequesterActivityChecker** — calls `mechMarketplace()` on the activity checker
2. **MechActivity** — if that fails, calls `agentMech()` on the mech activity contract
3. **Hardcoded fallback** — if both fail, uses `0x77af31De935740567Cf4fF1986D04B2c964A786a`

### Caching

- **OLAS price** — cached in memory with a configurable TTL (`PRICE_CACHE_TTL_SECONDS`, default 300s)
- **Staking metadata** — cached per metadata hash with a configurable TTL (`METADATA_CACHE_TTL_SECONDS`, default 3600s)
- Both use `threading.Lock` for thread safety

### Concurrent service task runner

`_run_service_tasks` runs operations across all configured services concurrently:

- Uses `asyncio.Semaphore` to limit concurrency (`SERVICE_CONCURRENCY`, default 4)
- Each service runs in a thread via `asyncio.to_thread` with a configurable timeout
- Partial failures are isolated — one failing service does not block others

### Error handling

- **Transient network errors** (`TimedOut`, `NetworkError`, `httpx.*` errors) are logged at INFO level and silently ignored to avoid spamming the chat during Telegram long-poll reconnects
- **All other errors** are logged and sent to the configured Telegram chat via `report_error`

## Useful commands (systemd)

```bash
make install  # install the service (systemd)
make start    # start the service (systemd)
make stop     # stop the service (systemd)
make logs     # see the service logs (systemd)
make update   # pull the latest version, reinstall and restart the service if needed (systemd)
```

## Development

### Run the tests

Run the full test suite:

```bash
poetry run pytest -q
```

Run a specific test file:

```bash
poetry run pytest -q tests/test_triton.py
```

### Lint and type checking

```bash
make formatters  # auto-format with black + isort
make code-check  # run isort, black, darglint, flake8, mypy, pylint
```

### CI

The repository includes a GitHub Actions workflow (`.github/workflows/common_checks.yml`) that runs linting and tests on push/PR to the `main` branch across Ubuntu, Windows, and macOS.

## What it looks like



<p align="center">
  <img src="images/screencap.jpg" alt="Imagen 1" width="40%"/>
  <img src="images/screencap2.jpg" alt="Imagen 2" width="40%"/>
</p>
