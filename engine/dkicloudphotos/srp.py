"""SRP-6a client for Apple's sign-in service (idmsa.apple.com).

Apple's variant, as used by the iCloud website:

* the 2048-bit group from RFC 5054 with g = 2 and SHA-256;
* k = H(N | pad(g)) and u = H(pad(A) | pad(B)) (RFC 5054 padding);
* x = H(salt | H(":" | P)) -- the account name is not part of x -- where
  P is not the password itself but PBKDF2-SHA256 of its SHA-256 digest
  ("s2k"), or of the hex form of that digest ("s2k_fo");
* M1 = H(H(N) xor H(pad(g)) | H(account) | salt | A | B | K), M2 = H(A | M1 | K)
  and K = H(S), where A, B and S are in their shortest big-endian form.

Clients differ on whether A, B and S are left-padded where the formulas
above use their shortest form; the two only disagree when a value starts
with a zero byte. :func:`SrpClient.process_challenge` reports such an
*ambiguous* handshake so the caller can start a fresh one instead of
risking a wrong proof, which Apple counts towards locking the account.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050"
    "A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50"
    "E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8"
    "55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B"
    "CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748"
    "544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB37861602790"
    "04E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8"
    "E9DBFBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F"
    "9E4AFF73"
)
N = int(N_HEX, 16)
G = 2
WIDTH = (N.bit_length() + 7) // 8        # 256 bytes
PROTOCOLS = ("s2k", "s2k_fo")
MAX_ITERATIONS = 5_000_000               # refuse to let a server burn our CPU


class SrpError(Exception):
    pass


def to_bytes(n: int) -> bytes:
    """Shortest big-endian form (zero is the empty string)."""
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def pad(data: bytes, width: int = WIDTH) -> bytes:
    return data.rjust(width, b"\0")


def H(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
    return h.digest()


def derive_password(password: str, salt: bytes, iterations: int, protocol: str) -> bytes:
    """Stretch the password the way the server announced in signin/init."""
    if not 0 < iterations <= MAX_ITERATIONS:
        raise SrpError(f"Unreasonable PBKDF2 iteration count {iterations}")
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    if protocol == "s2k_fo":
        digest = digest.hex().encode("ascii")
    elif protocol != "s2k":
        raise SrpError(f"Unsupported password protocol {protocol!r}")
    return hashlib.pbkdf2_hmac("sha256", digest, salt, iterations, 32)


@dataclass
class Proof:
    m1: bytes
    m2: bytes
    ambiguous: bool


class SrpClient:
    def __init__(self, secret: bytes | None = None) -> None:
        if secret is None:
            # A full-width A never depends on the padding convention.
            while True:
                raw = bytearray(os.urandom(WIDTH))
                raw[0] |= 0x80
                a = int.from_bytes(raw, "big")
                if len(to_bytes(pow(G, a, N))) == WIDTH:
                    break
        else:
            a = int.from_bytes(secret, "big")
        self._a = a
        self.A = pow(G, a, N)

    @property
    def public(self) -> bytes:
        return to_bytes(self.A)

    def process_challenge(self, account: str, key: bytes, salt: bytes, server_public: bytes) -> Proof | None:
        """Compute the proofs; None if the server's values are unsafe."""
        B = int.from_bytes(server_public, "big")
        if B % N == 0:
            return None
        k = int.from_bytes(H(to_bytes(N), pad(to_bytes(G))), "big")
        u = int.from_bytes(H(pad(to_bytes(self.A)), pad(to_bytes(B))), "big")
        if u == 0:
            return None
        x = int.from_bytes(H(salt, H(b":", key)), "big")
        v = pow(G, x, N)
        S = pow((B - k * v) % N, self._a + u * x, N)
        K = H(to_bytes(S))
        h_n, h_g = H(to_bytes(N)), H(pad(to_bytes(G)))
        m1 = H(
            bytes(p ^ q for p, q in zip(h_n, h_g)),
            H(account.encode("utf-8")),
            salt,
            to_bytes(self.A),
            to_bytes(B),
            K,
        )
        m2 = H(to_bytes(self.A), m1, K)
        ambiguous = any(len(to_bytes(value)) != WIDTH for value in (self.A, B, S))
        return Proof(m1=m1, m2=m2, ambiguous=ambiguous)
