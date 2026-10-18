"""Resource activation is opt-in, persistent, and transactional."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from sec.apps.cli.app import _resource_command_specs
from sec.apps.cli.resources import CliResourceManager


class CliResourceActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / f".resource-test-{uuid4().hex}"
        self.addCleanup(shutil.rmtree, self.root)
        skills = self.root / ".sec" / "skills"
        prompts = self.root / ".sec" / "prompts"
        skills.mkdir(parents=True)
        prompts.mkdir()
        (skills / "review.md").write_text(
            "---\ndescription: Review code\n---\nReview safely.", encoding="utf-8"
        )
        (prompts / "review.md").write_text("Review this.", encoding="utf-8")
        self.config = self.root / "settings" / "activation.json"

    def manager(self, *, persistent: bool = False) -> CliResourceManager:
        manager = CliResourceManager(
            self.root,
            user_root=self.root / "home",
            activation_path=self.config if persistent else None,
        )
        manager.reload()
        return manager

    def test_default_behavior_and_in_memory_filtering(self) -> None:
        manager = self.manager()
        self.assertEqual(manager.snapshot, manager.available_snapshot)
        self.assertTrue(manager.is_enabled("skill", "REVIEW"))
        self.assertTrue(manager.is_enabled("prompt", "review"))
        stale_resource = manager.get("skill", "review")
        self.assertIsNotNone(stale_resource)

        manager.set_enabled("skill", "REVIEW", False)

        self.assertFalse(manager.is_enabled("skill", "review"))
        self.assertEqual(manager.snapshot.skills, ())
        self.assertEqual(len(manager.available_snapshot.skills), 1)
        self.assertEqual(len(manager.snapshot.prompts), 1)
        self.assertEqual(manager.snapshot.issues, manager.available_snapshot.issues)
        self.assertIsNone(manager.get("skill", "review"))
        self.assertEqual(manager.reload().skills, ())
        self.assertFalse(self.config.exists())
        self.assertTrue(self.manager().is_enabled("skill", "review"))
        manager.set_enabled("skill", "review", True)
        self.assertEqual(manager.get("skill", "review"), stale_resource)

    def test_actual_persistence_constructor_read_and_reload(self) -> None:
        manager = self.manager(persistent=True)
        manager.set_enabled("skill", "review", False)
        manager.set_enabled("prompt", "review", False)
        self.assertEqual(
            json.loads(self.config.read_text(encoding="utf-8")),
            {"skill": {"review": False}, "prompt": {"review": False}},
        )

        restored = self.manager(persistent=True)
        self.assertFalse(restored.is_enabled("skill", "review"))
        self.assertFalse(restored.is_enabled("prompt", "review"))
        self.assertEqual(restored.reload().prompts, ())
        restored.set_enabled("prompt", "review", True)
        self.assertTrue(self.manager(persistent=True).is_enabled("prompt", "review"))
        self.assertFalse(self.manager(persistent=True).is_enabled("skill", "review"))

    def test_captured_handlers_recheck_enabled_state_through_get(self) -> None:
        manager = self.manager()
        captured = _resource_command_specs(manager)
        self.assertEqual(len(captured), 2)
        for kind, name, _description, handler in captured:
            with self.subTest(kind=kind):
                self.assertIsNotNone(handler(()).run_prompt)
                manager.set_enabled(kind, name, False)
                with patch.object(manager, "get", wraps=manager.get) as get:
                    outcome = handler(("must not run",))
                get.assert_called_once_with(kind, name)
                self.assertIsNone(outcome.run_prompt)
                self.assertEqual(outcome.display_kind, "error")
                self.assertIn("no longer available", outcome.message)
                manager.set_enabled(kind, name, True)
                self.assertIsNotNone(handler(()).run_prompt)

    def test_removed_resource_retains_setting_when_rediscovered(self) -> None:
        manager = self.manager(persistent=True)
        manager.set_enabled("skill", "review", False)
        path = self.root / ".sec" / "skills" / "review.md"
        content = path.read_text(encoding="utf-8")
        path.unlink()
        manager.reload()
        self.assertEqual(manager.available_snapshot.skills, ())
        self.assertIsNone(manager.get("skill", "review"))
        path.write_text(content, encoding="utf-8")
        manager.reload()
        self.assertFalse(manager.is_enabled("skill", "review"))
        self.assertIsNone(manager.get("skill", "review"))

    def test_invalid_kind_name_and_state_do_not_change_settings(self) -> None:
        manager = self.manager(persistent=True)
        for method in (manager.is_enabled, manager.set_enabled):
            for kind, name in (("extension", "review"), ("skill", "missing")):
                with self.subTest(method=method.__name__, kind=kind, name=name):
                    args = (kind, name, False) if method == manager.set_enabled else (kind, name)
                    with self.assertRaises(ValueError):
                        method(*args)
        with self.assertRaises(ValueError):
            manager.set_enabled("skill", "review", 0)
        with self.assertRaises(ValueError):
            manager.get("extension", "review")
        self.assertIsNone(manager.get("skill", "missing"))
        self.assertEqual(manager.snapshot, manager.available_snapshot)
        self.assertFalse(self.config.exists())

    def test_malformed_config_is_an_explicit_error(self) -> None:
        self.config.parent.mkdir()
        for source in (
            "{", "null", "[]", '{"extension": {}}', '{"skill": []}',
            '{"skill": {"review": 0}}', '{"prompt": {"review": "false"}}',
            '{"skill": {"INVALID": false}}',
        ):
            with self.subTest(source=source):
                self.config.write_text(source, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid resource activation config"):
                    self.manager(persistent=True)
        self.config.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "cannot read resource activation config"):
            self.manager(persistent=True)

    def test_failed_atomic_replace_rolls_back_and_cleans_up(self) -> None:
        manager = self.manager(persistent=True)
        manager.set_enabled("skill", "review", False)
        original = self.config.read_bytes()
        before = manager.snapshot
        with patch.object(Path, "replace", side_effect=OSError("replace denied")):
            with self.assertRaisesRegex(OSError, "cannot save resource activation config"):
                manager.set_enabled("skill", "review", True)
        self.assertEqual(manager.snapshot, before)
        self.assertFalse(manager.is_enabled("skill", "review"))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])
        self.assertFalse(self.manager(persistent=True).is_enabled("skill", "review"))

    def test_failed_write_rolls_back_and_cleans_up(self) -> None:
        manager = self.manager(persistent=True)
        with patch("sec.apps.cli.resources.json.dump", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "cannot save resource activation config"):
                manager.set_enabled("prompt", "review", False)
        self.assertTrue(manager.is_enabled("prompt", "review"))
        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.config.parent.iterdir()), [])
