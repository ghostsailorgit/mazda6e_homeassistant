"""Cryptographic helpers replicating what the Mazda 6e app does.

The app never sends e-mail address or password in clear text: both are
RSA-encrypted (PKCS#1 v1.5) with a public key that is embedded in the app.
On login the app also registers a freshly generated RSA key pair of its own
("pubKey"). The backend encrypts one-time serial numbers for remote-control
commands with it, and the app signs those commands with the private half.
We do the same, so the key generated at login is needed for remote control.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# Public key embedded in the official app (Android 1.2.3, RSAUtils).
SERVER_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAkyhr43cBPTJ3jLiYsmbUwUp74cMJ"
    "IOju5vqVzgtuK63Q99qV6iVT8wN5cXlyMtWI2mfOmhIao/fUN821im69MfOHsWXdqQEo5e9v"
    "654GPw+bju0pCphEPtD1I0VcyS34QkAu04urSun2U1q3Dr2OICLVWSnLa+01ioKxkaB0D209"
    "zXcls2eFQpvRAWm7xxVsoqzSwqp+neu5quOpn+eO/bW0TxcSQ8VZcDEUvadRTLSR0eOWgRuH"
    "IBiD2RGqPIPzKCm5A14q1qhxUZ8U0pmYe0Sx7eMy4RVe2iW7fnjc6pxTUMBkercSL26mevYo"
    "uuCKqyie+LVQAtGa29RMl/lyiwIDAQAB"
)

# PKCS#1 v1.5 with a 2048 bit key allows at most 256 - 11 bytes per block.
_MAX_BLOCK = 245


def encrypt_credential(value: str, public_key_b64: str = SERVER_PUBLIC_KEY) -> str:
    """Encrypt a credential the same way the app does (Android Base64.DEFAULT)."""
    key = serialization.load_der_public_key(base64.b64decode(public_key_b64))
    if not isinstance(key, rsa.RSAPublicKey):
        raise TypeError("Server key is not an RSA key")
    plain = value.encode("utf-8")
    encrypted = b"".join(
        key.encrypt(plain[i : i + _MAX_BLOCK], padding.PKCS1v15())
        for i in range(0, len(plain), _MAX_BLOCK)
    )
    # Android's Base64.DEFAULT wraps lines after 76 chars, like encodebytes.
    return base64.encodebytes(encrypted).decode("ascii")


def generate_key_pair() -> tuple[str, str]:
    """Return a new (public, private) RSA-2048 key pair as base64 DER strings."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_der = private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        base64.encodebytes(public_der).decode("ascii"),
        base64.encodebytes(private_der).decode("ascii"),
    )


def _load_private_key(private_key_b64: str) -> rsa.RSAPrivateKey:
    key = serialization.load_der_private_key(
        base64.b64decode("".join(private_key_b64.split())), password=None
    )
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Control key is not an RSA key")
    return key


def decrypt_serial(value_b64: str, private_key_b64: str) -> str:
    """Decrypt the one-time serial number the backend encrypts for our key."""
    key = _load_private_key(private_key_b64)
    ciphertext = base64.b64decode("".join(value_b64.split()))
    return key.decrypt(ciphertext, padding.PKCS1v15()).decode().strip()


# Fields the app leaves out of the signature.
_UNSIGNED_KEYS = frozenset({"sign", "command", "class"})


def sign_payload(payload: dict[str, object], private_key_b64: str) -> str:
    """Sign a control command like the app.

    The canonical string is ``key=value`` pairs sorted by key and joined with
    ``&``; booleans are lower case. Signature: RSA PKCS#1 v1.5 with SHA-256.
    """
    parts = []
    for key in sorted(payload):
        if key in _UNSIGNED_KEYS:
            continue
        value = payload[key]
        if isinstance(value, bool):
            value = str(value).lower()
        parts.append(f"{key}={value}")
    signature = _load_private_key(private_key_b64).sign(
        "&".join(parts).encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return base64.encodebytes(signature).decode("ascii")
