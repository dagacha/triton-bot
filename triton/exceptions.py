class TritonError(Exception):
    """Base exception for Triton."""


class InsufficientFundsError(TritonError):
    """Raised when there are insufficient funds for a transaction."""


class ContractExecutionError(TritonError):
    """Raised when a smart contract execution fails."""


class RateLimitError(TritonError):
    """Raised when a rate limit is encountered."""
