"""Core Markdown discovery, metadata and atomic refresh contracts."""

from hashlib import sha256
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from sec.apps.cli.resources import CliResourceManager
from sec.resources import ResourceManager
from sec.resources.markdown import MAX_RESOURCE_BYTES


class CoreResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f".core-resources-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.manager = ResourceManager(self.root, user_root=self.root / "user")

    def skill(self, metadata: str = "", content: str = "Review safely.") -> Path:
        path = self.root / ".sec" / "skills" / "review.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ndescription: Review code\n{metadata}---\n{content}", encoding="utf-8",
        )
        return path

    def test_alias_and_default_metadata(self) -> None:
        self.assertIs(ResourceManager, CliResourceManager)
        self.skill()
        card = self.manager.reload().skills[0]
        self.assertEqual(card.activation, "auto")
        self.assertEqual(card.match, ())
        self.assertEqual(card.when_to_use, "")
        self.assertEqual(card.digest, sha256(b"Review safely.").hexdigest())

    def test_required_literal_case_insensitive_triggers(self) -> None:
        self.skill("activation: required\nwhen_to_use: Review requests\nmatch: Review Code, API, api\n")
        card = self.manager.reload(strict=True).skills[0]
        self.assertEqual(card.match, ("review code", "api"))
        self.assertEqual(card.when_to_use, "Review requests")
        self.assertTrue(card.matches("Please REVIEW CODE"))
        self.assertFalse(card.matches("fix a spelling error"))
        self.manager.set_enabled("skill", "review", False)
        self.assertEqual(self.manager.snapshot.skills, ())
        self.assertEqual(self.manager.available_snapshot.skills, (card,))

    def test_manual_and_invalid_activation_or_triggers(self) -> None:
        self.skill("activation: manual\n")
        self.assertEqual(self.manager.reload(strict=True).skills[0].activation, "manual")
        for metadata in (
            "activation: always\n", "activation: required\n",
            "activation: required\nmatch: ,\n", "match: one,,two\n",
            "activation: auto\nactivation: manual\n",
        ):
            with self.subTest(metadata=metadata):
                self.skill(metadata)
                with self.assertRaises(ValueError):
                    self.manager.reload(strict=True)

    def test_strict_reload_preserves_previous_snapshot(self) -> None:
        self.skill()
        before = self.manager.reload()
        self.skill("activation: invalid\n", "Changed.")
        with self.assertRaisesRegex(ValueError, "activation"):
            self.manager.reload(strict=True)
        self.assertEqual(self.manager.snapshot, before)
        tolerant = self.manager.reload()
        self.assertEqual(tolerant.skills, ())
        self.assertEqual(tolerant.issues[0].severity, "error")

    def test_workspace_override_is_notice_not_error(self) -> None:
        self.skill()
        user = self.root / "user" / "skills" / "review.md"
        user.parent.mkdir(parents=True)
        user.write_text("---\ndescription: User\n---\nUser rules.", encoding="utf-8")
        snapshot = self.manager.reload(strict=True)
        self.assertEqual(snapshot.skills[0].content, "Review safely.")
        self.assertEqual(snapshot.issues[0].severity, "notice")
        self.assertIn("overridden", snapshot.issues[0].message)

    def test_symlink_and_oversize_rejected(self) -> None:
        path = self.skill()
        outside = self.root / "outside.md"
        outside.write_text("---\ndescription: External\n---\nExternal.", encoding="utf-8")
        path.unlink()
        path.symlink_to(outside.resolve())
        with self.assertRaisesRegex(ValueError, "outside"):
            self.manager.reload(strict=True)
        path.unlink()
        path.write_bytes(b"x" * (MAX_RESOURCE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.manager.reload(strict=True)
