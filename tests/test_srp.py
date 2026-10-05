"""SRP-6a checks against vectors from the reference Python `srp` library.

The vectors were produced by srp 1.0.22 configured the way pyicloud
configures it (SHA-256, 2048-bit group, rfc5054_enable(), no_username_in_x())
and published by the icloud-linux project (crates/icloud-api/tests). A proof
that is off by one byte makes Apple answer "wrong password", so these pin
the maths exactly.

Run with:  python -m unittest tests.test_srp -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from dkicloudphotos import srp  # noqa: E402

SALT = bytes(range(16))
PASSWORD = "correct horse battery staple"

SECRET = bytes.fromhex(
    "91030a11181f262d343b424950575e656c737a81888f969da4abb2b9c0c7ced5"
    "dce3eaf1f8ff060d141b222930373e454c535a61686f767d848b9299a0a7aeb5"
    "bcc3cad1d8dfe6edf4fb020910171e252c333a41484f565d646b727980878e95"
    "9ca3aab1b8bfc6cdd4dbe2e9f0f7fe050c131a21282f363d444b525960676e75"
    "7c838a91989fa6adb4bbc2c9d0d7dee5ecf3fa01080f161d242b323940474e55"
    "5c636a71787f868d949ba2a9b0b7bec5ccd3dae1e8eff6fd040b121920272e35"
    "3c434a51585f666d747b828990979ea5acb3bac1c8cfd6dde4ebf2f900070e15"
    "1c232a31383f464d545b626970777e858c939aa1a8afb6bdc4cbd2d9e0e7eef5"
)
SERVER_PUBLIC = bytes.fromhex(
    "1985fa3ec760b9b46bba90ba440636fb4710ce72fee19404b5d16d0f79f922a1"
    "2f9ed9f416f5ae24857d5a63541189eed19fa8562de3402e68c4f57e9ce00d63"
    "3d786f84db2cc37a7ff5a148ac79d72bedcb1ecffa557397ce333234bd7acdc7"
    "c3decc12253ac025697a749accf2a86ba23fb69b04c64407facdd1eaa9c08722"
    "8a21eb781cee1dc8c8df6f55ff5763286ed7c22dc740236ca0b4800a6dba6cb1"
    "33aa3695f392a25d3ff6c4b2e3fc5ef5c7632d368bfa04f544a97f01a7eaaab1"
    "b59fa099dae53618435d8de6235e9e1b6f9e9999e2effb07c7185e35e2988e1f"
    "3a8e6b24dfec8108cbc4ebd110251086308a31bd9dfa298ea27575235c85f9cc"
)
CLIENT_PUBLIC = bytes.fromhex(
    "985f3f3220745ccf12c56f0f3a9303af9f78c2b85b6a4cf9231b55496daabde0"
    "7632437681d9596f331eb9f2634f8b35b4f0cabd6cbc54b83c1ee0e49f138fb4"
    "0ba7f3ab257502f61753e80d79ae7f6a8377da9d6e9b05f928f13f77a472a423"
    "623a56ebcd694f77a7164ce2705f3992a0580e35f5a5584a4853f0fa0a84cb80"
    "c00caae370c16b1510bcd3e059bd05c584ae8cfd500ae1db33ce48e74aa5a678"
    "9982ef20a3b0d4bf5022a4da3fc3fe74b214125403425f06d2a4683f1ee013e1"
    "a80613698fa0b5402e3b2a76ec15a210e8e9899023ff9a25062e0a77b20cad00"
    "55eb64f6eb8c39070ef563c7debece0859381c47cbf78034a905a283186ed1ba"
)


class ReferenceVectors(unittest.TestCase):
    def test_password_stretching(self):
        self.assertEqual(
            srp.derive_password(PASSWORD, SALT, 20_000, "s2k").hex(),
            "acadb60b74c60e9faee03393f08584f322e7c0a456db31c7f11276f736599b27",
        )
        self.assertEqual(
            srp.derive_password(PASSWORD, SALT, 20_000, "s2k_fo").hex(),
            "272cfa7988c03b2100d3dcd40767c3d48df3b58ea2a9af2b2043e1ab88656c2b",
        )

    def test_handshake(self):
        client = srp.SrpClient(SECRET)
        self.assertEqual(client.public, CLIENT_PUBLIC)
        key = srp.derive_password(PASSWORD, SALT, 20_000, "s2k_fo")
        proof = client.process_challenge("user@example.com", key, SALT, SERVER_PUBLIC)
        self.assertIsNotNone(proof)
        self.assertEqual(proof.m1.hex(), "c0638970cbce771f95ad5a126bf0fd05a25f77b159de8a0f26e45954b4e424a9")
        self.assertEqual(proof.m2.hex(), "fadf0cf144f3ef04bdf35fa78bafc0554d08b63771add68177bbd947b7b22f19")


class Safety(unittest.TestCase):
    def test_unsafe_server_values_are_refused(self):
        client = srp.SrpClient(SECRET)
        self.assertIsNone(client.process_challenge("u", b"k", b"s", b"\0"))
        self.assertIsNone(client.process_challenge("u", b"k", b"s", srp.to_bytes(srp.N)))

    def test_bad_protocol_and_iterations(self):
        with self.assertRaises(srp.SrpError):
            srp.derive_password("pw", b"salt", 10, "s3k")
        with self.assertRaises(srp.SrpError):
            srp.derive_password("pw", b"salt", 0, "s2k")
        with self.assertRaises(srp.SrpError):
            srp.derive_password("pw", b"salt", srp.MAX_ITERATIONS + 1, "s2k")

    def test_random_clients_are_full_width(self):
        for _ in range(20):
            client = srp.SrpClient()
            self.assertEqual(len(client.public), srp.WIDTH)

    def test_ambiguous_handshakes_are_flagged(self):
        client = srp.SrpClient(SECRET)
        short_b = srp.to_bytes(pow(srp.G, 12345, srp.N) >> 16)  # shorter than N
        proof = client.process_challenge("u", b"k" * 32, SALT, short_b)
        self.assertTrue(proof is None or proof.ambiguous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
