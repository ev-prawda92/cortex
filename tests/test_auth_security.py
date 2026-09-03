import os
from unittest import TestCase
from unittest.mock import patch

import auth


class AuthSecurityTests(TestCase):
    def test_new_password_hash_uses_current_work_factor(self):
        value = auth.hash_password("correct horse battery staple")
        self.assertTrue(value.startswith("pbkdf2:sha256:600000$"))
        self.assertTrue(auth.verify_password("correct horse battery staple", value))
        self.assertFalse(auth.verify_password("wrong", value))

    def test_legacy_password_hash_remains_valid(self):
        with patch.object(auth, "PBKDF2_ITERATIONS", 100_000):
            legacy = auth.hash_password("legacy password")
        self.assertTrue(auth.verify_password("legacy password", legacy))

    def test_token_rejects_header_tampering(self):
        token = auth.create_token("user-1", "person@example.com")
        header, payload, signature = token.split(".")
        tampered = auth._b64url_encode(b'{"alg":"none","typ":"JWT"}')
        self.assertIsNone(auth.decode_token(f"{tampered}.{payload}.{signature}"))
        self.assertIsNotNone(auth.decode_token(token))
