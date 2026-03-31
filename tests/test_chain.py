"""Tests for triton.chain module"""
import datetime
from decimal import Decimal
from http import HTTPStatus
from unittest.mock import Mock, patch, MagicMock, mock_open
import pytz
from operate.operate_types import Chain
from web3.exceptions import ABIFunctionNotFound

from triton.chain import (
    get_native_balance,
    get_wrapped_native_balance,
    load_contract,
    get_olas_balance,
    get_staking_metadata,
    get_mech_request_count,
    get_staking_status,
    get_olas_price,
    get_slots,
    web3
)
from triton.constants import LOCAL_TIMEZONE


class TestGetNativeBalance:
    """Tests for get_native_balance function"""
    
    @patch('triton.chain.web3')
    def test_get_native_balance_success(self, mock_web3):
        """Test successful native balance retrieval"""
        mock_web3.eth.get_balance.return_value = 1000000000000000000  # 1 ETH in wei
        mock_web3.to_checksum_address = lambda x: x
        mock_web3.from_wei.return_value = 1.0
        
        result = get_native_balance("0x1234567890abcdef1234567890abcdef12345678")
        
        assert result == 1.0
        mock_web3.eth.get_balance.assert_called_once_with("0x1234567890abcdef1234567890abcdef12345678")
        mock_web3.from_wei.assert_called_once_with(1000000000000000000, "ether")
    
    @patch('triton.chain.web3')
    def test_get_native_balance_zero(self, mock_web3):
        """Test zero native balance"""
        mock_web3.eth.get_balance.return_value = 0
        mock_web3.from_wei.return_value = 0.0
        
        result = get_native_balance("0x1234567890abcdef1234567890abcdef12345678")
        
        assert result == 0.0


class TestLoadContract:
    """Tests for load_contract function"""
    
    @patch('builtins.open', new_callable=mock_open, read_data='{"abi": [{"name": "test"}]}')
    @patch('triton.chain.web3')
    def test_load_contract_with_abi_key(self, mock_web3, mock_file):
        """Test loading contract with ABI key"""
        mock_contract = MagicMock()
        mock_web3.eth.contract.return_value = mock_contract
        mock_web3.to_checksum_address = lambda x: x
        
        result = load_contract("0x1234567890abcdef1234567890abcdef12345678", "test", True)
        
        assert result == mock_contract
        mock_web3.eth.contract.assert_called_once_with(
            address="0x1234567890abcdef1234567890abcdef12345678",
            abi=[{"name": "test"}]
        )


class TestGetWrappedNativeBalance:
    """Tests for get_wrapped_native_balance function"""

    @patch('triton.chain.load_contract')
    def test_get_wrapped_native_balance_uses_token_decimals(
        self, mock_load_contract
    ):
        """Test wrapped native balance is converted using ERC20 decimals."""
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 300000
        mock_contract.functions.decimals.return_value.call.return_value = 6
        mock_load_contract.return_value = mock_contract

        result = get_wrapped_native_balance(
            "0x1234567890abcdef1234567890abcdef12345678", Chain.GNOSIS
        )

        assert result == Decimal("0.3")
        mock_contract.functions.balanceOf.assert_called_once_with(
            "0x1234567890abcdef1234567890abcdef12345678"
        )
        mock_contract.functions.decimals.assert_called_once_with()
    
    @patch('builtins.open', new_callable=mock_open, read_data='[{"name": "test"}]')
    @patch('triton.chain.web3')
    def test_load_contract_without_abi_key(self, mock_web3, mock_file):
        """Test loading contract without ABI key"""
        mock_contract = MagicMock()
        mock_web3.eth.contract.return_value = mock_contract
        mock_web3.to_checksum_address = lambda x: x
        
        result = load_contract("0x1234567890abcdef1234567890abcdef12345678", "test", False)
        
        assert result == mock_contract
        mock_web3.eth.contract.assert_called_once_with(
            address="0x1234567890abcdef1234567890abcdef12345678",
            abi=[{"name": "test"}]
        )


class TestGetOlasBalance:
    """Tests for get_olas_balance function"""
    
    @patch('triton.chain.load_contract')
    def test_get_olas_balance_success(self, mock_load_contract):
        """Test successful OLAS balance retrieval"""
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 1000000000000000000  # 1 OLAS
        mock_load_contract.return_value = mock_contract
        
        result = get_olas_balance("0x1234567890abcdef1234567890abcdef12345678")
        
        assert result == 1000000000000000000
        mock_contract.functions.balanceOf.assert_called_once_with("0x1234567890abcdef1234567890abcdef12345678")
    
    @patch('triton.chain.load_contract')
    def test_get_olas_balance_zero(self, mock_load_contract):
        """Test zero OLAS balance"""
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 0
        mock_load_contract.return_value = mock_contract
        
        result = get_olas_balance("0x1234567890abcdef1234567890abcdef12345678")
        
        assert result == 0


class TestGetMechRequestCount:
    """Tests for get_mech_request_count function"""
    
    @patch('triton.chain.load_contract')
    def test_get_mech_request_count_success(self, mock_load_contract):
        """Test successful mech request count retrieval"""
        mock_contract = MagicMock()
        mock_contract.functions.mapRequestsCounts.return_value.call.return_value = 5
        mock_load_contract.return_value = mock_contract
        
        result = get_mech_request_count(
            "0x1234567890abcdef1234567890abcdef12345678",
            "0xabcdef1234567890abcdef1234567890abcdef12"
        )
        
        assert result == 5
        mock_contract.functions.mapRequestsCounts.assert_called_once_with(
            "0xabcdef1234567890abcdef1234567890abcdef12"
        )
    
    @patch('triton.chain.load_contract')
    def test_get_mech_request_count_fallback(self, mock_load_contract):
        """Test fallback to mapRequestCounts on exception"""
        mock_contract = MagicMock()
        mock_contract.functions.mapRequestsCounts.return_value.call.side_effect = ABIFunctionNotFound("Not found")
        mock_contract.functions.mapRequestCounts.return_value.call.return_value = 3
        mock_load_contract.return_value = mock_contract
        
        result = get_mech_request_count(
            "0x1234567890abcdef1234567890abcdef12345678",
            "0xabcdef1234567890abcdef1234567890abcdef12"
        )
        
        assert result == 3
        mock_contract.functions.mapRequestCounts.assert_called_once_with(
            "0xabcdef1234567890abcdef1234567890abcdef12"
        )


class TestGetStakingStatus:
    """Tests for get_staking_status function"""
    
    @patch('triton.chain.get_mech_request_count')
    @patch('triton.chain.load_contract')
    @patch('triton.chain.wei_to_olas')
    @patch('triton.chain.requests.get')
    def test_get_staking_status_success(self, mock_requests, mock_wei_to_olas, mock_load_contract, mock_get_mech_request_count):
        """Test successful staking status retrieval"""
        # Mock contracts
        mock_staking_contract = MagicMock()
        mock_activity_contract = MagicMock()
        
        # Mock contract responses
        mock_staking_contract.functions.mapServiceInfo.return_value.call.return_value = [
            "0x59536E0e06FE394Aa82a4d40B0087b5f19841E2f",
            "0x67f6086f87D7698F0a2C37530B0f3549c304D04E",
            "1752808320",
            "15216712962976289600",
            "0"
        ]
        mock_staking_contract.functions.getServiceInfo.return_value.call.return_value = [
            "0x59536E0e06FE394Aa82a4d40B0087b5f19841E2f",
            "0x67f6086f87D7698F0a2C37530B0f3549c304D04E",
            [
                7240,
                93
            ],
            "1752808320",
            "15216712962976289600",
            "0"
        ]
        mock_staking_contract.functions.livenessPeriod.return_value.call.return_value = 86400
        mock_staking_contract.functions.tsCheckpoint.return_value.call.return_value = 1753007240
        mock_activity_contract.functions.livenessRatio.return_value.call.return_value = 462962962962960
        
        # Mock load_contract to return appropriate contracts
        def load_contract_side_effect(address, abi_name, has_abi_key=True):
            if abi_name == "staking_token":
                return mock_staking_contract
            elif abi_name == "mech_activity":
                return mock_activity_contract
            return MagicMock()
        
        mock_load_contract.side_effect = load_contract_side_effect
        mock_get_mech_request_count.return_value = 126
        mock_wei_to_olas.return_value = "1.00 OLAS"
        
        mock_response = Mock()
        mock_response.status_code = HTTPStatus.OK
        mock_response.json.return_value = {
            "name": "Staking Program 1",
            "description": "Test staking program",
            "available_staking_slots": 100
        }
        mock_requests.return_value = mock_response

        result = get_staking_status(
            "0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB",
            "0x9c7F6103e3a72E4d1805b9C683Ea5B370Ec1a99f",
            "0x29e3f37CB7a4f4F00a07Ea7BE956006163809298",
            1259,
            "0x59536E0e06FE394Aa82a4d40B0087b5f19841E2f"
        )
        
        assert result["accrued_rewards"] == "1.00 OLAS"
        assert result["mech_requests_this_epoch"] == 33
        assert result["required_mech_requests"] == 40
        assert result["epoch_end"] == datetime.datetime.fromtimestamp(
            1753007240 + 86400,
            pytz.timezone(LOCAL_TIMEZONE),
        ).strftime("%Y-%m-%d %H:%M:%S %Z")
        assert result["metadata"] == {
            "name": "Staking Program 1",
            "description": "Test staking program",
            "available_staking_slots": 100,
        }


class TestGetOlasPrice:
    """Tests for get_olas_price function"""
    
    @patch("triton.chain._price_cache", {"value": None, "expires_at": 0.0})
    @patch('triton.chain.requests')
    def test_get_olas_price_success(self, mock_requests):
        """Test successful OLAS price retrieval"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"autonolas": {"usd": 1.23}}
        mock_requests.get.return_value = mock_response
        
        result = get_olas_price()
        
        assert result == 1.23
        mock_requests.get.assert_called_once()
    
    @patch("triton.chain._price_cache", {"value": None, "expires_at": 0.0})
    @patch('triton.chain.requests')
    @patch('triton.chain.logger')
    def test_get_olas_price_error(self, mock_logger, mock_requests):
        """Test OLAS price retrieval with API error"""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_requests.get.return_value = mock_response
        
        result = get_olas_price()
        
        assert result is None
        mock_logger.error.assert_called_once_with(mock_response)

    @patch("triton.chain._price_cache", {"value": None, "expires_at": 0.0})
    @patch("triton.chain.requests")
    def test_get_olas_price_uses_cache(self, mock_requests):
        """Test OLAS price retrieval is cached."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"autonolas": {"usd": 1.23}}
        mock_requests.get.return_value = mock_response

        first = get_olas_price()
        second = get_olas_price()

        assert first == 1.23
        assert second == 1.23
        mock_requests.get.assert_called_once()


class TestGetStakingMetadata:
    """Tests for staking metadata cache."""

    @patch("triton.chain._metadata_cache", {})
    @patch("triton.chain.requests.get")
    def test_get_staking_metadata_uses_cache(self, mock_get):
        """Metadata fetch should be cached per metadata hash."""
        mock_response = MagicMock()
        mock_response.status_code = HTTPStatus.OK
        mock_response.json.return_value = {"name": "Cached Program"}
        mock_get.return_value = mock_response

        first = get_staking_metadata("abc123")
        second = get_staking_metadata("abc123")

        assert first == {"name": "Cached Program"}
        assert second == {"name": "Cached Program"}
        mock_get.assert_called_once()


class TestGetSlots:
    """Tests for get_slots function"""
    
    @patch('triton.chain.STAKING_CONTRACTS', {
        "Test Contract 1": {"address": "0x1234567890abcdef1234567890abcdef12345678", "slots": 10},
        "Test Contract 2": {"address": "0xabcdef1234567890abcdef1234567890abcdef12", "slots": 20}
    })
    @patch('triton.chain.load_contract')
    @patch('triton.chain.web3')
    def test_get_slots_success(self, mock_web3, mock_load_contract):
        """Test successful slots retrieval"""
        mock_web3.to_checksum_address.side_effect = lambda x: x.upper()
        
        # Mock contracts
        mock_contract_1 = MagicMock()
        mock_contract_1.functions.getServiceIds.return_value.call.return_value = [1, 2, 3]  # 3 services
        
        mock_contract_2 = MagicMock()
        mock_contract_2.functions.getServiceIds.return_value.call.return_value = [1, 2, 3, 4, 5]  # 5 services
        
        mock_load_contract.side_effect = [mock_contract_1, mock_contract_2]
        
        result = get_slots()
        
        assert result == {
            "Test Contract 1": 7,  # 10 - 3
            "Test Contract 2": 15  # 20 - 5
        }
        assert mock_load_contract.call_count == 2
    
    @patch('triton.chain.STAKING_CONTRACTS', {})
    def test_get_slots_empty_contracts(self):
        """Test get_slots with empty contracts dictionary"""
        result = get_slots()
        
        assert result == {}


class TestWeb3Integration:
    """Integration tests for web3 setup"""
    
    def test_web3_instance_exists(self):
        """Test that web3 instance is properly initialized"""
        assert web3 is not None
        assert hasattr(web3, 'eth')
        assert hasattr(web3, 'from_wei')
