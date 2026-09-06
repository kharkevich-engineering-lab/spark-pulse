"""The master key, and what happens when it is absent, wrong, or rotated badly.

Secrets used to be a 0600 file on one machine. They are becoming rows in a
database that a replica, a backup and a ``pg_dump`` can all read, so they are
encrypted before they are written. Everything asserted here is a property an
operator depends on but cannot see: that two encryptions of one password do
not look alike, that a row someone edited fails loudly instead of decrypting
to something else, and that a missing key stops a secret from being stored
rather than storing it in the clear.
"""

from __future__ import annotations

import base64
import re

import pytest

from spark_pulse import crypto

SECRET = "hf_aVeryRealLookingHuggingFaceToken"


@pytest.fixture
def key(monkeypatch):
    """A master key for the duration of one test, and its base64 form."""
    generated = crypto.generate_key()
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, generated)
    return generated


@pytest.fixture
def no_key(monkeypatch):
    """An instance with nothing configured — the upgrade-day default."""
    monkeypatch.delenv(crypto.MASTER_KEY_ENV, raising=False)


# ── The round trip ──────────────────────────────────────────────────────────


def test_a_secret_comes_back_exactly_as_it_went_in(key):
    assert crypto.decrypt(crypto.encrypt(SECRET)) == SECRET


def test_an_empty_secret_round_trips_rather_than_being_treated_as_absent(key):
    """A cleared secret is a value, not a missing one.

    ``delete_secret`` and ``save_secret("")`` are different operations in the
    config API this will back, so the empty string has to survive the trip.
    """
    assert crypto.decrypt(crypto.encrypt("")) == ""


def test_a_secret_with_non_ascii_characters_survives(key):
    """Passphrases are not ASCII, and the stored form is.

    The bytes go through base64 and the text through UTF-8; getting either
    direction wrong shows up here rather than on some operator's password.
    """
    original = "pässwörd–with–ünicode–✓"
    assert crypto.decrypt(crypto.encrypt(original)) == original


def test_a_freshly_generated_key_is_immediately_usable(monkeypatch):
    """The command the documentation gives an operator has to actually work."""
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_key())
    assert crypto.decrypt(crypto.encrypt(SECRET)) == SECRET


def test_generate_key_produces_thirty_two_distinct_random_bytes():
    first, second = crypto.generate_key(), crypto.generate_key()
    assert len(base64.b64decode(first)) == crypto.KEY_BYTES
    assert first != second, "two calls returned the same key"


# ── Nonce hygiene ───────────────────────────────────────────────────────────


def test_encrypting_the_same_secret_twice_reuses_neither_nonce_nor_ciphertext(key):
    """A repeated nonce under one GCM key exposes the authentication subkey.

    It is also the visible half of the problem: identical ciphertexts would
    tell anyone with read access to the table which two accounts share a
    password, without decrypting anything.
    """
    first, second = crypto.encrypt(SECRET), crypto.encrypt(SECRET)

    assert first != second
    assert first.split(".")[1] != second.split(".")[1], "the nonce repeated"
    assert first.split(".")[2] != second.split(".")[2]
    assert crypto.decrypt(first) == crypto.decrypt(second) == SECRET


def test_many_encryptions_of_one_secret_never_repeat_a_nonce(key):
    nonces = {crypto.encrypt(SECRET).split(".")[1] for _ in range(200)}
    assert len(nonces) == 200


# ── What the stored form leaks ──────────────────────────────────────────────


def test_the_stored_form_contains_neither_the_plaintext_nor_the_key(key):
    """The whole reason this module exists, stated as an assertion.

    Checked against the key in every encoding an operator might have pasted,
    because a stored form that happened to embed the base64 key would be no
    better than the plaintext file it replaces.
    """
    token = crypto.encrypt(SECRET)
    raw_key = base64.b64decode(key)

    assert SECRET not in token
    assert key not in token
    assert key.rstrip("=") not in token
    assert base64.urlsafe_b64encode(raw_key).decode().rstrip("=") not in token
    assert raw_key.hex() not in token
    assert raw_key not in token.encode("utf-8")


def test_the_stored_form_announces_its_version_before_anything_encoded(key):
    """A ``SELECT`` has to be readable without decoding anything.

    The tag leads so that the day a ``v2`` exists, telling the two apart is a
    string comparison rather than an inference from length.
    """
    token = crypto.encrypt(SECRET)

    assert token.startswith("v1.")
    assert crypto.is_encrypted(token)
    assert re.fullmatch(r"v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token), token


def test_a_legacy_plaintext_value_is_not_mistaken_for_a_token(key):
    """The one-time JSON import has to tell the two apart without decrypting.

    Deciding by catching a decryption failure would make a genuine wrong-key
    error look like a legacy row, and re-encrypt someone's ciphertext as if it
    were the password itself.
    """
    assert not crypto.is_encrypted(SECRET)
    assert not crypto.is_encrypted("")
    assert crypto.is_encrypted(crypto.encrypt(SECRET))


# ── Wrong key, and tampering ────────────────────────────────────────────────


def test_a_secret_written_under_one_key_is_refused_under_another(monkeypatch, key):
    """The badly-rotated-key case, which must not look like corruption."""
    token = crypto.encrypt(SECRET)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_key())

    with pytest.raises(crypto.DecryptionError) as raised:
        crypto.decrypt(token)
    assert crypto.MASTER_KEY_ENV in str(raised.value)
    assert "altered" in str(raised.value)


def test_a_wrong_key_raises_a_different_error_from_a_missing_one(monkeypatch, key):
    """An operator has to know which of the two mistakes they made.

    Both are "secrets stopped working after I touched the environment", and
    the fixes — set the variable, versus put the old key back — have nothing
    in common.
    """
    token = crypto.encrypt(SECRET)

    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_key())
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token)

    monkeypatch.delenv(crypto.MASTER_KEY_ENV)
    with pytest.raises(crypto.MasterKeyError):
        crypto.decrypt(token)

    assert not issubclass(crypto.MasterKeyError, crypto.DecryptionError)
    assert not issubclass(crypto.DecryptionError, crypto.MasterKeyError)


def test_an_edited_ciphertext_fails_authentication_instead_of_decrypting(key):
    """A row changed in the database — by a bad migration or by someone with
    write access and no read access — must not decrypt to chosen bytes."""
    version, nonce, ciphertext = crypto.encrypt(SECRET).split(".")
    flipped = ("B" if ciphertext[0] != "B" else "C") + ciphertext[1:]

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(".".join((version, nonce, flipped)))


def test_an_edited_nonce_fails_authentication(key):
    version, nonce, ciphertext = crypto.encrypt(SECRET).split(".")
    flipped = ("B" if nonce[0] != "B" else "C") + nonce[1:]

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(".".join((version, flipped, ciphertext)))


def test_a_truncated_ciphertext_is_rejected_rather_than_half_decrypted(key):
    version, nonce, ciphertext = crypto.encrypt(SECRET).split(".")

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(".".join((version, nonce, ciphertext[:-4])))


def test_a_truncated_nonce_is_reported_as_corruption_not_as_a_new_format(key):
    """v1 has only ever emitted twelve bytes, so a short one is an edit."""
    version, nonce, ciphertext = crypto.encrypt(SECRET).split(".")

    with pytest.raises(crypto.DecryptionError) as raised:
        crypto.decrypt(".".join((version, nonce[:8], ciphertext)))
    assert "nonce" in str(raised.value)


def test_relabelling_a_token_as_a_later_version_does_not_open_it(key):
    """The version tag is the AEAD's associated data, so it cannot be moved.

    Without that binding, a stored value could be re-tagged and handed to a
    future parser that expects different bytes behind the same key.
    """
    _, nonce, ciphertext = crypto.encrypt(SECRET).split(".")

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(".".join(("v2", nonce, ciphertext)))


def test_something_that_is_not_a_token_at_all_is_refused(key):
    for value in ("", SECRET, "v1.", "v1.abc", "v1.a.b.c", "v2.abc.def"):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(value)


def test_a_token_whose_base64_is_damaged_is_reported_as_corrupt(key):
    _, nonce, ciphertext = crypto.encrypt(SECRET).split(".")

    with pytest.raises(crypto.DecryptionError) as raised:
        crypto.decrypt(".".join(("v1", nonce, ciphertext + "*!")))
    assert "corrupt" in str(raised.value)


# ── No key, and bad keys ────────────────────────────────────────────────────


def test_without_a_key_encrypting_refuses_rather_than_storing_plaintext(no_key):
    """The documented choice: refuse to store, never fall back to the clear.

    A plaintext fallback is silent — the operator who typoed the variable name
    gets a working system and a readable database — and it is ambiguous,
    because nothing reading the column afterwards can tell which values were
    protected.
    """
    with pytest.raises(crypto.MasterKeyError) as raised:
        crypto.encrypt(SECRET)

    message = str(raised.value)
    assert crypto.MASTER_KEY_ENV in message
    assert "generate_key" in message, "the message must say what to run"


def test_without_a_key_decrypting_says_the_key_is_missing(no_key):
    with pytest.raises(crypto.MasterKeyError):
        crypto.decrypt("v1.AAAAAAAAAAAAAAAA.AAAAAAAAAAAAAAAA")


def test_a_caller_can_ask_whether_secrets_can_be_stored_before_trying(
    monkeypatch, no_key
):
    """So the refusal reads as a policy, not as a traceback from a save."""
    assert crypto.is_configured() is False

    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_key())
    assert crypto.is_configured() is True


def test_an_exported_but_empty_key_counts_as_absent(monkeypatch):
    """``Environment=SPARK_PULSE_MASTER_KEY=`` in a unit file produces one.

    Reporting that as "wrong length" would send the operator looking for a
    truncated paste that does not exist.
    """
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, "   ")

    assert crypto.is_configured() is False
    with pytest.raises(crypto.MasterKeyError) as raised:
        crypto.encrypt(SECRET)
    assert "not set" in str(raised.value)


def test_a_key_of_the_wrong_length_is_named_as_such(monkeypatch):
    """The commonest paste error, and it must not be silently padded."""
    monkeypatch.setenv(
        crypto.MASTER_KEY_ENV, base64.b64encode(b"\x01" * 16).decode("ascii")
    )

    with pytest.raises(crypto.MasterKeyError) as raised:
        crypto.encrypt(SECRET)
    assert "16 bytes" in str(raised.value)
    assert str(crypto.KEY_BYTES) in str(raised.value)


def test_a_key_that_is_not_base64_is_named_as_such(monkeypatch):
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, "not base64 at all !!")

    with pytest.raises(crypto.MasterKeyError) as raised:
        crypto.encrypt(SECRET)
    assert "base64" in str(raised.value)


def test_a_key_pasted_with_surrounding_whitespace_still_works(monkeypatch):
    """``$(...)`` in a shell brings a trailing newline with it."""
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, f"\n  {crypto.generate_key()}\t\n")

    assert crypto.decrypt(crypto.encrypt(SECRET)) == SECRET


def test_a_key_in_the_url_safe_alphabet_or_without_padding_still_works(monkeypatch):
    """Secret managers and shells hand back ``-``/``_``, and strip ``=``.

    Refusing a key that is materially correct helps nobody, and the operator
    debugging it has no way to see which character offended.
    """
    raw = base64.b64decode(crypto.generate_key())

    monkeypatch.setenv(
        crypto.MASTER_KEY_ENV, base64.urlsafe_b64encode(raw).decode("ascii")
    )
    token = crypto.encrypt(SECRET)

    monkeypatch.setenv(
        crypto.MASTER_KEY_ENV,
        base64.b64encode(raw).decode("ascii").rstrip("="),
    )
    assert crypto.decrypt(token) == SECRET, "the same key was read as a different one"


def test_the_key_is_re_read_rather_than_cached_for_the_life_of_the_process(
    monkeypatch, key
):
    """A cached key would survive a rotation the supervisor already performed,
    and would make every test after the first one here lie."""
    token = crypto.encrypt(SECRET)
    rotated = crypto.generate_key()

    monkeypatch.setenv(crypto.MASTER_KEY_ENV, rotated)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token)

    monkeypatch.setenv(crypto.MASTER_KEY_ENV, key)
    assert crypto.decrypt(token) == SECRET


def test_every_failure_here_is_catchable_as_one_category(no_key):
    """Callers that only need "secrets are unavailable" get one except clause."""
    assert issubclass(crypto.MasterKeyError, crypto.CryptoError)
    assert issubclass(crypto.DecryptionError, crypto.CryptoError)

    with pytest.raises(crypto.CryptoError):
        crypto.encrypt(SECRET)
