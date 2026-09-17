"""The browser's install request, and an operator's private key, parsed here."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from spark_pulse.agent import bootstrap
from spark_pulse.agent.bootstrap_transport import (
    UnusableKey,
    generate_keypair,
    keypair_from_private_pem,
)
from spark_pulse.agent.onboarding import AUTH_METHODS, parse_request


def _ed25519(passphrase: bytes | None = None, *, pem: bool = False) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=(
            serialization.PrivateFormat.PKCS8
            if pem
            else serialization.PrivateFormat.OpenSSH
        ),
        encryption_algorithm=(
            serialization.BestAvailableEncryption(passphrase)
            if passphrase
            else serialization.NoEncryption()
        ),
    )


class TestKeys:
    def test_a_plain_openssh_key_is_read_and_its_public_half_derived(self):
        pair = generate_keypair("me")
        again = keypair_from_private_pem(pair.private_openssh)
        assert again.public_openssh.split()[1] == pair.public_openssh.split()[1]
        assert again.fingerprint == pair.fingerprint

    def test_an_encrypted_openssh_key_is_unlocked_with_its_passphrase(self):
        locked = _ed25519(b"open sesame")
        pair = keypair_from_private_pem(locked, passphrase="open sesame")
        # What is carried onward is the *unlocked* key: the SSH library is
        # never handed a passphrase it might prompt for.
        assert b"ENCRYPTED" not in pair.private_openssh
        serialization.load_ssh_private_key(pair.private_openssh, password=None)

    def test_an_encrypted_key_without_a_passphrase_says_so(self):
        with pytest.raises(UnusableKey, match="encrypted; supply its passphrase"):
            keypair_from_private_pem(_ed25519(b"open sesame"))

    def test_a_wrong_passphrase_does_not_pretend_to_know_which_it_was(self):
        with pytest.raises(UnusableKey, match="does not unlock"):
            keypair_from_private_pem(_ed25519(b"open sesame"), passphrase="nope")

    def test_a_pem_key_is_accepted_too(self):
        pair = keypair_from_private_pem(_ed25519(pem=True))
        assert pair.public_openssh.startswith("ssh-ed25519 ")

    def test_an_encrypted_pem_key_is_accepted_too(self):
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        locked = rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
        )
        assert keypair_from_private_pem(
            locked, passphrase="pw"
        ).public_openssh.startswith("ssh-rsa ")

    def test_something_that_is_not_a_key_is_named_as_such(self):
        with pytest.raises(UnusableKey, match="not an OpenSSH or PEM private key"):
            keypair_from_private_pem(b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB public")


class TestParseRequest:
    BASE = {"username": "alex", "host_key_fingerprint": "SHA256:abc"}

    def test_the_three_methods_are_the_only_ones(self):
        assert AUTH_METHODS == ("password", "key", "control_plane_key")

    def test_a_password_request(self):
        parsed = parse_request({**self.BASE, "auth": "password", "password": "pw"})
        assert parsed.auth == "password"
        assert parsed.password == "pw"
        assert parsed.private_key is None
        assert parsed.port == 22
        assert parsed.scope == "auto"

    def test_a_key_request_carries_the_key_as_bytes_and_the_passphrase(self):
        parsed = parse_request(
            {**self.BASE, "auth": "key", "private_key": "-----BEGIN", "passphrase": "p"}
        )
        assert parsed.private_key == b"-----BEGIN"
        assert parsed.passphrase == "p"
        assert parsed.password is None

    def test_a_password_sent_with_a_key_request_is_dropped(self):
        """One method per request; a stray password is not kept around."""
        parsed = parse_request(
            {**self.BASE, "auth": "key", "private_key": "k", "password": "stray"}
        )
        assert parsed.password is None

    def test_empty_optional_secrets_are_none_not_empty_strings(self):
        parsed = parse_request(
            {
                **self.BASE,
                "auth": "control_plane_key",
                "sudo_password": "",
                "passphrase": "",
            }
        )
        assert parsed.sudo_password is None
        assert parsed.passphrase is None

    def test_port_and_scope_and_control_host_are_taken(self):
        parsed = parse_request(
            {
                **self.BASE,
                "auth": "control_plane_key",
                "port": "2222",
                "scope": "system",
                "control_host": " 10.0.0.1 ",
            }
        )
        assert (parsed.port, parsed.scope, parsed.control_host) == (
            2222,
            "system",
            "10.0.0.1",
        )

    @pytest.mark.parametrize(
        "body,message",
        [
            (
                {"auth": "password", "password": "x", "host_key_fingerprint": "f"},
                "username",
            ),
            (
                {"username": "a", "auth": "password", "password": "x"},
                "host_key_fingerprint",
            ),
            (
                {"username": "a", "auth": "nope", "host_key_fingerprint": "f"},
                "auth must be",
            ),
            (
                {"username": "a", "auth": "password", "host_key_fingerprint": "f"},
                "password is required",
            ),
            (
                {"username": "a", "auth": "key", "host_key_fingerprint": "f"},
                "private key is required",
            ),
            (
                {
                    "username": "a",
                    "auth": "control_plane_key",
                    "host_key_fingerprint": "f",
                    "port": "ssh",
                },
                "port must be a number",
            ),
            (
                {
                    "username": "a",
                    "auth": "control_plane_key",
                    "host_key_fingerprint": "f",
                    "port": 70000,
                },
                "between 1 and 65535",
            ),
            (
                {
                    "username": "a",
                    "auth": "control_plane_key",
                    "host_key_fingerprint": "f",
                    "scope": "root",
                },
                "scope must be",
            ),
        ],
    )
    def test_refusals_name_the_field(self, body, message):
        with pytest.raises(ValueError, match=message):
            parse_request(body)


# ── The confirmed host key, written where OpenSSH will look ─────────────────
#
# The browser shows a fingerprint, the operator confirms it, and the installer
# refuses to send a byte if the node then offers a different key. That
# knowledge used to end inside AsyncSSH, so rsync — OpenSSH, strict checking —
# refused every bootstrapped node with *No ED25519 host key is known* and the
# workaround on a real cluster was ``ssh-keyscan``, which trusts whatever
# answers. What follows is the key that was actually confirmed being recorded.


def _host_key(host: str = "10.0.0.7", port: int = 22, blob: bytes = b"a-host-key"):
    from spark_pulse.agent.bootstrap_transport import HostKey

    return HostKey(host=host, port=port, algorithm="ssh-ed25519", blob=blob)


class TestTheConfirmedHostKeyIsRecorded:
    def test_the_key_lands_in_the_control_planes_known_hosts(self):
        from spark_pulse.tools.ssh import trusted_entries

        key = _host_key()
        assert bootstrap.trust_host_key_for("10.0.0.7", key) is True
        assert trusted_entries("10.0.0.7") == [key.openssh]

    def test_recording_it_again_is_not_a_change(self):
        assert bootstrap.trust_host_key_for("10.0.0.7", _host_key()) is True
        assert bootstrap.trust_host_key_for("10.0.0.7", _host_key()) is False

    def test_the_report_says_it_happened(self):
        report = bootstrap.InstallReport(host="10.0.0.7", username="alex", name="n")
        bootstrap.trust_host_key_for("10.0.0.7", _host_key(), report=report)
        assert any("host key" in step for step in report.steps)

    def test_a_file_that_cannot_be_written_is_a_note_not_a_failure(self, monkeypatch):
        """An install that worked is not undone by a known_hosts we cannot write."""
        import importlib

        ssh_mod = importlib.import_module("spark_pulse.tools.ssh")

        def refuse(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(ssh_mod, "_write_known_hosts", refuse)
        report = bootstrap.InstallReport(host="10.0.0.7", username="alex", name="n")
        assert (
            bootstrap.trust_host_key_for("10.0.0.7", _host_key(), report=report)
            is False
        )
        assert any("could not record" in step for step in report.steps)

    def test_a_non_default_port_is_recorded_as_ssh_writes_it(self):
        from spark_pulse.tools.ssh import known_hosts_path

        bootstrap.trust_host_key_for("10.0.0.7", _host_key(port=2222), port=2222)
        assert known_hosts_path().read_text().startswith("[10.0.0.7]:2222 ")
