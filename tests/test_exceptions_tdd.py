import pytest
from unittest.mock import Mock, patch
from triton.service import _transact_with_receipt
from triton.exceptions import ContractExecutionError, InsufficientFundsError, RateLimitError

def test_transact_with_receipt_raises_contract_execution_error():
    """Test that _transact_with_receipt raises ContractExecutionError for non-retryable errors."""
    ledger_api = Mock()
    crypto = Mock()
    tx_builder = Mock(return_value={"gas": 21000})
    
    # Mock ledger_api.send_signed_transaction to raise a non-retryable error
    # We'll simulate a contract revert which often shows up in error messages
    ledger_api.send_signed_transaction.side_effect = Exception("Contract revert")
    
    # Also mock the deadline and retries to avoid long wait
    with patch("triton.service.datetime") as mock_datetime, \
         patch("triton.service.gnosis_utils.ON_CHAIN_INTERACT_RETRIES", 1), \
         patch("triton.service.gnosis_utils.ON_CHAIN_INTERACT_SLEEP", 0):
        
        # We need to mock datetime.now().timestamp() to ensure we don't loop forever
        mock_datetime.now.return_value.timestamp.return_value = 1000
        # We need to ensure the deadline is also handled. 
        # Since we are patching datetime, we might need to be careful.
        
        with pytest.raises(ContractExecutionError):
            _transact_with_receipt(ledger_api, crypto, tx_builder)

def test_transact_with_receipt_raises_insufficient_funds_error():
    """Test that _transact_with_receipt raises InsufficientFundsError for insufficient funds."""
    ledger_api = Mock()
    crypto = Mock()
    tx_builder = Mock(return_value={"gas": 21000})
    
    ledger_api.send_signed_transaction.side_effect = Exception("insufficient funds")
    
    with patch("triton.service.datetime") as mock_datetime, \
         patch("triton.service.gnosis_utils.ON_CHAIN_INTERACT_RETRIES", 1), \
         patch("triton.service.gnosis_utils.ON_CHAIN_INTERACT_SLEEP", 0):
        
        mock_datetime.now.return_value.timestamp.return_value = 1000
        
        with pytest.raises(InsufficientFundsError):
            _transact_with_receipt(ledger_api, crypto, tx_builder)

def test_transact_with_receipt_raises_rate_limit_error():
    """Test that _transact_with_receipt raises RateLimitError for rate limits."""
    ledger_api = Mock()
    crypto = Mock()
    tx_builder = Mock(return_value={"gas": 21000})
    
    ledger_api.send_signed_transaction.side_effect = Exception("rate limit exceeded")
    
    with patch("triton.service.datetime") as mock_datetime, \
         patch("triton.service.gnosis_utils.ON_CHAIN_INTERACT_RETRIES", 1), \
         patch("triton.service.gnosis_utils.ON_CHAIN_INTERACT_SLEEP", 0):
        
        mock_datetime.now.return_value.timestamp.return_value = 1000
        
        with pytest.raises(RateLimitError):
            _transact_with_receipt(ledger_api, crypto, tx_builder)
