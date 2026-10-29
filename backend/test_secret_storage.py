"""Failed key saves must preserve prior credentials, file contents and names."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent_config
import secretbox
from roles import ROSTER


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, key, value):
        self.values[(service, key)] = value

    def get_password(self, service, key):
        return self.values.get((service, key))

    def delete_password(self, service, key):
        self.values.pop((service, key), None)


class SecretStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "agents.json"
        self.backend = MemoryKeyring()
        for target, values in ((agent_config, {"CONFIG_PATH": str(self.path)}),
                               (secretbox, {"os": SimpleNamespace(name="posix")})):
            context = patch.multiple(target, **values)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(secretbox, "_keyring_backend", return_value=self.backend)
        context.start()
        self.addCleanup(context.stop)

    def test_key_rotation_and_clear_preserve_other_members(self):
        agent_config.update_agent_config(98, llm={"api_key": "first-secret"})
        agent_config.update_agent_config(99, llm={"api_key": "other-secret"})
        original = json.loads(self.path.read_text())
        agent_config.update_agent_config(98, name="new name")
        self.assertEqual(json.loads(self.path.read_text())["98"]["llm"]["api_key"], original["98"]["llm"]["api_key"])
        self.assertEqual(len(self.backend.values), 2)
        agent_config.update_agent_config(98, llm={"api_key": "replacement-secret"})
        self.assertEqual(len(self.backend.values), 2)
        self.assertNotIn("first-secret", self.backend.values.values())
        self.assertEqual(agent_config.resolve_llm(99)["api_key"], "other-secret")
        self.assertNotIn("replacement-secret", self.path.read_text())
        agent_config.update_agent_config(98, llm={"api_key": "__CLEAR__"})
        self.assertEqual(len(self.backend.values), 1)

    def test_config_replace_failure_cleans_new_entries_and_preserves_old_key(self):
        agent_config.update_agent_config(99, llm={"api_key": "original-secret"})
        before = self.path.read_bytes()
        with patch.object(agent_config.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                agent_config.update_agent_config(99, llm={"api_key": "new-secret"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.backend.values.values()), ["original-secret"])
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_unavailable_store_rejects_new_keys_without_changing_names(self):
        before_name = ROSTER[1]["name"]
        with patch.object(secretbox, "_keyring_backend", side_effect=ValueError("locked")):
            with self.assertRaises(ValueError):
                agent_config.update_agent_config(1, name="should not apply", llm={"api_key": "never-write-this"})
        self.assertEqual(ROSTER[1]["name"], before_name)
        self.assertFalse(self.path.exists())

    def test_locked_existing_reference_is_preserved_during_non_secret_edit(self):
        agent_config.update_agent_config(99, llm={"api_key": "original-secret"})
        protected = json.loads(self.path.read_text())["99"]["llm"]["api_key"]
        with patch.object(secretbox, "_keyring_backend", side_effect=ValueError("locked")):
            agent_config.update_agent_config(99, name="renamed")
        self.assertEqual(json.loads(self.path.read_text())["99"]["llm"]["api_key"], protected)
        self.assertEqual(agent_config.resolve_llm(99)["api_key"], "original-secret")

    def test_legacy_plaintext_is_migrated_without_secret_in_json(self):
        self.path.write_text(json.dumps({"99": {"llm": {"api_key": "legacy-secret"}}}))
        agent_config._migrate_plaintext_keys()
        self.assertNotIn("legacy-secret", self.path.read_text())
        self.assertEqual(agent_config.resolve_llm(99)["api_key"], "legacy-secret")

    def test_failed_legacy_migration_leaves_original_file_untouched(self):
        self.path.write_text(json.dumps({"99": {"llm": {"api_key": "legacy-secret"}}}))
        before = self.path.read_bytes()
        with patch.object(secretbox, "_keyring_backend", side_effect=ValueError("locked")):
            agent_config._migrate_plaintext_keys()
        self.assertEqual(self.path.read_bytes(), before)

    def test_api_save_failure_returns_redacted_error_and_keeps_old_config(self):
        from fastapi.testclient import TestClient
        import main
        agent_config.update_agent_config(99, llm={"api_key": "old-secret"})
        before = self.path.read_bytes()
        client = TestClient(main.app)
        self.addCleanup(client.close)
        with patch.object(secretbox, "_keyring_backend", side_effect=ValueError("locked")):
            response = client.put("/api/agents/1", json={"api_key": "new-sensitive-secret"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("new-sensitive-secret", response.text)
        self.assertEqual(self.path.read_bytes(), before)

    def test_user_cannot_supply_a_protected_reference_as_a_new_key(self):
        for value in ("keyring:some-reference", "dpapi:some-ciphertext"):
            with self.assertRaises(ValueError):
                agent_config.update_agent_config(99, llm={"api_key": value})
        self.assertFalse(self.path.exists())


class NativeBackendSelectionTest(unittest.TestCase):
    def test_plaintext_and_null_plugins_are_rejected(self):
        for module in ("keyrings.alt.file", "keyring.backends.null", "custom.plugin"):
            backend_type = type("Backend", (), {"__module__": module, "priority": 10})
            with patch.dict(sys.modules, {"keyring": SimpleNamespace(get_keyring=lambda: backend_type())}):
                with self.assertRaises(ValueError):
                    secretbox._keyring_backend()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI failure path")
    def test_windows_encryption_failure_never_returns_plaintext(self):
        import ctypes
        with patch.object(ctypes.windll.crypt32, "CryptProtectData", side_effect=OSError("unavailable")):
            with self.assertRaises(ValueError):
                secretbox.encrypt_value("never-write-this")
