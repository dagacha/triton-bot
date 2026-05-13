import os
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import asyncio

from triton.triton import _run_script_command

def test_run_script_command_uses_correct_env_fallbacks():
    """Test that _run_script_command does not use hardcoded /home/ubuntu fallbacks."""
    script_path = Path("/tmp/test_script.sh")
    
    # We want to simulate an environment where HOME and USER are NOT set
    # and verify that the function doesn't fall back to /home/ubuntu or ubuntu
    with patch.dict(os.environ, {}, clear=True), \
         patch("triton.triton.subprocess.run") as mock_run, \
         patch("triton.triton.Path.parent", return_value=Path("/tmp")):
        
        # Mock subprocess.run return value
        mock_run.return_value = Mock(returncode=0, stdout="success", stderr="")
        
        # We need to ensure the script_path exists for the function to work 
        # (it calls script_path.parent)
        # Actually it just uses it in subprocess.run
        
        _run_script_command(script_path)
        
        # Get the env argument passed to subprocess.run
        called_args, called_kwargs = mock_run.call_args
        env = called_kwargs.get("env", {})
        
        # If the code is still using hardcoded fallbacks, 
        # these assertions will fail.
        # We check that HOME is not /home/ubuntu and USER is not ubuntu
        # if they aren't in os.environ.
        # Note: Since we cleared os.environ, if it uses os.getenv("HOME", "/home/ubuntu"), 
        # it will be "/home/ubuntu".
        
        assert env.get("HOME") != "/home/ubuntu", f"Expected HOME not to be '/home/ubuntu', got {env.get('HOME')}"
        assert env.get("USER") != "ubuntu", f"Expected USER not to be 'ubuntu', got {env.get('USER')}"
