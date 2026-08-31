"""Encrypted storage for the credentials agents use to reach their data sources.

The rule this module exists to enforce: a secret never lives in an agent's
config. Config is snapshotted into an immutable AgentVersion row on every
change, served to the UI, diffed, and rolled back — so a secret placed there is
copied into permanent history the moment anyone edits anything, and served to
every caller who can read the agent.

Config holds a reference instead: an opaque id plus a hint like "****4f21" so a
human can tell one key from another. The value itself lives here, encrypted at
rest, and is decrypted only at the moment a run actually needs it.

Key management: CORTEX_ENCRYPTION_KEY in the environment is the supported
production path. Absent that, a key is generated once and written to
.cortex-key beside the app so local development works without ceremony — that
file is gitignored, and losing it means the stored secrets cannot be recovered,
which is the correct failure mode.
"""

import os
import warnings

from cryptography.fernet import Fernet, InvalidToken

_KEY_ENV = "CORTEX_ENCRYPTION_KEY"
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cortex-key")

_fernet = None


def _load_key() -> bytes:
    key = os.environ.get(_KEY_ENV, "").strip()
    if key:
        return key.encode()

    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as fh:
            return fh.read().strip()

    key = Fernet.generate_key()
    # 0600: the key is the only thing standing between a stolen database file
    # and every credential in it.
    fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    warnings.warn(
        f"No {_KEY_ENV} set — generated one at {_KEY_FILE}. Set {_KEY_ENV} in "
        "production; if this file is lost, stored credentials cannot be decrypted.",
        RuntimeWarning, stacklevel=2)
    return key


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Empty in, empty out."""
    if not plaintext:
        return ""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a stored secret.

    Raises ValueError on a value this key cannot open — a wrong or rotated key,
    or a tampered row. Callers should let that surface rather than falling back
    to an empty string, which would silently turn an authenticated request into
    an unauthenticated one.
    """
    if not ciphertext:
        return ""
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "could not decrypt this credential — the encryption key has "
            "changed or the stored value is corrupt"
        ) from e


def hint(plaintext: str) -> str:
    """A non-reversible label so a human can tell two keys apart.

    Four characters of a secret is not enough to reconstruct it and is what
    every console shows; anything shorter than eight characters gets no tail at
    all, because revealing half of a short secret is not a hint.
    """
    if not plaintext:
        return ""
    if len(plaintext) < 8:
        return "****"
    return "****" + plaintext[-4:]
