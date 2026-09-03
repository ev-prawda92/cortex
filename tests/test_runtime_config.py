import os
from unittest import TestCase
from unittest.mock import patch

from runtime_config import load_runtime_config, production_errors


class RuntimeConfigTests(TestCase):
    def test_development_has_safe_local_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = load_runtime_config()
        self.assertEqual(config.environment, "development")
        self.assertFalse(config.authz_fail_closed)
        self.assertEqual(production_errors(config), [])

    def test_unsafe_production_configuration_is_rejected(self):
        with patch.dict(os.environ, {"CORTEX_ENV": "production"}, clear=True):
            errors = production_errors(load_runtime_config())
        self.assertGreaterEqual(len(errors), 6)

    def test_complete_production_configuration_passes(self):
        values = {
            "CORTEX_ENV": "production",
            "SECRET_KEY": "s" * 64,
            "CORTEX_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "DATABASE_URL": "postgresql://cortex:secret@db/cortex",
            "BASE_URL": "https://cortex.example.com",
            "CORS_ORIGINS": "https://cortex.example.com",
            "TRUSTED_HOSTS": "cortex.example.com",
            "SEED_SAMPLE_AGENTS": "false",
            "CORTEX_AUTHZ_FAIL_CLOSED": "true",
            "ALLOW_SIGNUP": "false",
            "CORTEX_BOOTSTRAP_TOKEN": "b" * 64,
        }
        with patch.dict(os.environ, values, clear=True):
            errors = production_errors(load_runtime_config())
        self.assertEqual(errors, [])
