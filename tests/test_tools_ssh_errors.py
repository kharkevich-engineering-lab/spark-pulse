"""Tests for SSH error classification."""

from __future__ import annotations

import pytest

from spark_pulse.tools.ssh import (
    OpenSSHClient,
    SSHError,
    SSHErrorType,
    SSHResult,
)


class TestSSHErrorClassification:
    @pytest.mark.parametrize("stderr,expected", [
        ("Permission denied (publickey).", SSHErrorType.AUTH),
        ("Permission denied (keyboard-interactive).", SSHErrorType.AUTH),
        ("Permission denied (other).", SSHErrorType.PERMISSION_DENIED),
        ("Host key verification failed.", SSHErrorType.HOST_KEY),
        ("Connection timed out.", SSHErrorType.TIMEOUT),
        ("Connection refused.", SSHErrorType.NETWORK),
        ("No route to host.", SSHErrorType.NETWORK),
        ("Some unknown error.", SSHErrorType.UNKNOWN),
    ])
    def test_classify_ssh_error(self, stderr, expected):
        result = OpenSSHClient._classify_ssh_error(255, stderr)
        assert result == expected

    def test_case_insensitive(self):
        # Should match regardless of case
        result = OpenSSHClient._classify_ssh_error(255, "PERMISSION DENIED (PUBLICKEY).")
        assert result == SSHErrorType.AUTH

    def test_empty_stderr(self):
        result = OpenSSHClient._classify_ssh_error(1, "")
        assert result == SSHErrorType.UNKNOWN


class TestSSHError:
    def test_str_representation(self):
        error = SSHError(
            error_type=SSHErrorType.AUTH,
            host="10.0.0.1",
            message="Invalid credentials",
        )
        assert str(error) == "SSHError(auth: 10.0.0.1 - Invalid credentials)"

    def test_with_stderr(self):
        error = SSHError(
            error_type=SSHErrorType.TIMEOUT,
            host="10.0.0.2",
            message="Timed out",
            stderr="Connection timed out",
        )
        assert error.stderr == "Connection timed out"


class TestSSHResult:
    def test_ok_true(self):
        result = SSHResult(returncode=0, stdout="output", stderr="")
        assert result.ok is True

    def test_ok_false(self):
        result = SSHResult(returncode=1, stdout="", stderr="error")
        assert result.ok is False
