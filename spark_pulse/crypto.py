"""Envelope encryption for the secrets that now live in the database.

Config and secrets are moving out of ``~/.config/spark-pulse/*.json`` and into
the store :mod:`spark_pulse.db` owns, because in a distributed deployment the
database is the primary and the filesystem is only a local cache. That move
takes three things with it that were previously protected by a 0600 file on
one machine: the HuggingFace token, the OIDC client secret, and eventually the
cluster CA's private key. On PostgreSQL a 0600 file no longer exists, and the
rows are reachable by anything holding a connection string — a backup, a
replica, a read-only analytics grant, a ``pg_dump`` in someone's home
directory. So the values are encrypted before they are written, and the key
that opens them is *not* in the database.

**The key is an environment variable, per app instance.** ``SPARK_PULSE_MASTER_KEY``
holds 32 base64 bytes for AES-256-GCM. Keeping it out of the database is the
entire point: a key stored beside the ciphertext it protects is decoration.
Every instance of the app that must read a given secret needs the same key,
which is why it is an operator-supplied constant rather than something this
program generates on first boot — a self-generated per-node key would make the
second node unable to read what the first one wrote.

**AEAD, not encryption alone.** GCM authenticates as well as conceals, so a row
edited in the database — by a botched migration or by someone with write
access and no read access — fails loudly on the next read instead of decrypting
to attacker-chosen bytes. The version tag is fed in as associated data, so
relabelling a ``v1`` token as some future ``v2`` also fails authentication
rather than being handed to a parser that expects a different shape.

**No key configured means secrets are refused, never stored in the clear.**
The alternative — fall back to plaintext so the app keeps working — is the
worse failure in every direction. It is silent: an operator who typoed the
variable name gets a running system that reports nothing wrong and a database
full of readable tokens, and the moment they later *set* the key those rows
are still plaintext, because nothing ever went back for them. It is also
ambiguous: a plaintext value and a ciphertext value are both just strings in
the column, so no reader can tell which it is holding, and "is this deployment
encrypted at rest?" stops having an answer. Refusing turns that into an error
at the one moment an operator can still act on it — the first attempt to save
a secret — with a message telling them what to run. :func:`is_configured` is
how a caller asks in advance so it can refuse politely, in its own vocabulary,
rather than by propagating an exception.

Generating a key is :func:`generate_key`, so the documentation can hand an
operator one command::

    python -c "from spark_pulse.crypto import generate_key; print(generate_key())"
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "MASTER_KEY_ENV",
    "KEY_BYTES",
    "NONCE_BYTES",
    "VERSION",
    "CryptoError",
    "MasterKeyError",
    "DecryptionError",
    "generate_key",
    "is_configured",
    "is_encrypted",
    "encrypt",
    "decrypt",
]

#: Where the key comes from. Namespaced like every other variable this program
#: reads, so it is greppable in a unit file and unmistakable in an environment
#: dump.
MASTER_KEY_ENV = "SPARK_PULSE_MASTER_KEY"

#: AES-256. Not negotiable per-instance: a key length that varied by
#: deployment would mean the stored form does not say which cipher opens it.
KEY_BYTES = 32

#: 96 bits, the size GCM is specified for. Anything else forces the cipher
#: through an extra hashing step and buys nothing.
NONCE_BYTES = 12

#: The version tag every stored value carries. Present so that changing the
#: format later is a branch on a known string rather than a guess about what
#: the bytes used to mean — the failure this avoids is a future ``v2`` reader
#: silently mis-parsing a ``v1`` row and returning plausible garbage.
VERSION = "v1"

_SEPARATOR = "."


class CryptoError(Exception):
    """Base for everything here, so a caller can catch the category."""


class MasterKeyError(CryptoError):
    """The key is absent, undecodable, or the wrong length.

    Distinct from :class:`DecryptionError` because the operator response is
    different: this one means *fix the environment*, and the other means *you
    have the wrong key, or the data has been altered*. An operator who rotates
    a key badly must be able to tell those apart from the message alone.
    """


class DecryptionError(CryptoError):
    """The ciphertext did not authenticate under this key.

    Wrong key, truncated value, or tampering — GCM cannot distinguish them and
    neither can we, so the message says all three rather than guessing at one.
    """


def generate_key() -> str:
    """A fresh master key, base64, ready to paste into an environment file."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _raw_key() -> str:
    """The configured key as the operator typed it, or "" for absent.

    An exported-but-empty variable counts as absent: ``Environment=SPARK_PULSE_MASTER_KEY=``
    in a unit file, or an unset shell variable expanded into a wrapper script,
    both produce one, and treating it as a zero-length key would report
    "wrong length" for what is really "not configured".
    """
    return os.environ.get(MASTER_KEY_ENV, "").strip()


def is_configured() -> bool:
    """Whether a key is present at all. Not whether it is valid.

    A caller uses this to decide *before* it offers to store a secret, so the
    refusal reads as "this instance has no master key" rather than as a
    traceback from the save. A malformed key still raises on use, because a
    key that is present and wrong is an operator error to surface, not a
    reason to behave as if none were set.
    """
    return bool(_raw_key())


def master_key() -> bytes:
    """The decoded key, or :class:`MasterKeyError` saying which way it is wrong.

    Deliberately not cached. The saving is one base64 decode per secret read,
    and a cache would need invalidating the moment anything changed the
    environment — which is exactly what the tests do, and what a supervisor
    does when it re-execs the process with a rotated key.
    """
    raw = _raw_key()
    if not raw:
        raise MasterKeyError(
            f"{MASTER_KEY_ENV} is not set, so secrets cannot be encrypted or read. "
            "Generate one with: python -c "
            '"from spark_pulse.crypto import generate_key; print(generate_key())"'
        )
    # Accept the URL-safe alphabet and missing padding as well as the strict
    # form: operators paste these through shells, YAML and secret managers,
    # and several of those hand back ``-``/``_`` or strip trailing ``=``.
    # Rejecting a key that is materially correct helps nobody.
    normalised = raw.replace("-", "+").replace("_", "/")
    normalised += "=" * (-len(normalised) % 4)
    try:
        key = base64.b64decode(normalised, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyError(
            f"{MASTER_KEY_ENV} is not valid base64. Expected {KEY_BYTES} "
            f"base64-encoded bytes; generate one with "
            'python -c "from spark_pulse.crypto import generate_key; '
            'print(generate_key())"'
        ) from exc
    if len(key) != KEY_BYTES:
        raise MasterKeyError(
            f"{MASTER_KEY_ENV} decodes to {len(key)} bytes; AES-256-GCM needs "
            f"exactly {KEY_BYTES}. A key of the wrong length is usually a "
            "truncated paste."
        )
    return key


def _b64encode(data: bytes) -> str:
    """URL-safe and unpadded, so a token survives being a URL or a shell word.

    The stored form ends up in log lines, error messages and occasionally a
    query string; ``+``, ``/`` and ``=`` are the three characters that would
    need quoting in one of those places.
    """
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    """Strict, because a discarded character is corruption worth naming.

    The lenient decoders silently drop anything outside the alphabet, which
    turns an edited row into a shorter blob and defers the complaint to the
    authentication tag — where the message would blame the key rather than
    the value that was actually damaged.
    """
    padded = text + "=" * (-len(text) % 4)
    return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` into the self-describing stored form.

    The form is ``v1.<nonce>.<ciphertext+tag>``, both parts unpadded URL-safe
    base64. The version leads so it is readable in a ``SELECT`` without
    decoding anything, and it is also the AEAD's associated data, which is
    what stops a stored value from being relabelled as a later format.

    The nonce is fresh randomness on every call and never a counter. A counter
    restarts at zero when a replica reboots or a database is restored from a
    backup, and two messages sharing a nonce under one GCM key do not merely
    leak their relationship — they expose the authentication subkey, after
    which any ciphertext can be forged.
    """
    key = master_key()
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(
        nonce, plaintext.encode("utf-8"), VERSION.encode("ascii")
    )
    return _SEPARATOR.join((VERSION, _b64encode(nonce), _b64encode(ciphertext)))


def is_encrypted(value: str) -> bool:
    """Whether ``value`` looks like something :func:`decrypt` should be given.

    For the one-time import of the old JSON secrets, which has to tell a
    legacy plaintext value from a row that has already been through here, and
    must not do it by calling :func:`decrypt` and catching the failure — that
    would make a genuine wrong-key error indistinguishable from a legacy row
    and quietly re-encrypt someone's ciphertext as if it were a password.
    """
    return value.startswith(VERSION + _SEPARATOR)


def decrypt(token: str) -> str:
    """Recover the plaintext, or say precisely which way it could not be.

    :class:`MasterKeyError` if this instance has no usable key;
    :class:`DecryptionError` if the value is not a token of a version we know,
    or does not authenticate under the key we have.
    """
    key = master_key()
    parts = token.split(_SEPARATOR)
    if len(parts) != 3 or parts[0] != VERSION:
        raise DecryptionError(
            f"not an encrypted value this build understands: expected a "
            f"{VERSION!r} token of three {_SEPARATOR!r}-separated parts"
        )
    _, nonce_b64, ciphertext_b64 = parts
    try:
        nonce = _b64decode(nonce_b64)
        ciphertext = _b64decode(ciphertext_b64)
    except (binascii.Error, ValueError) as exc:
        raise DecryptionError(
            "encrypted value is corrupt: its base64 does not decode"
        ) from exc
    if len(nonce) != NONCE_BYTES:
        # A short nonce is a truncated or edited row, not a different format —
        # v1 has only ever emitted twelve bytes.
        raise DecryptionError(
            f"encrypted value is corrupt: nonce is {len(nonce)} bytes, "
            f"expected {NONCE_BYTES}"
        )
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, VERSION.encode("ascii"))
    except InvalidTag as exc:
        raise DecryptionError(
            f"encrypted value failed authentication: either {MASTER_KEY_ENV} is "
            "not the key this value was written with, or the stored value has "
            "been altered"
        ) from exc
    return plaintext.decode("utf-8")
