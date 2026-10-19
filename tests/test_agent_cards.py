"""Strict agent-card parsing, containment and atomic catalog reload."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from sec.agent.card import AgentCapabilities, AgentCard
from sec.resources import AgentCatalog
from sec.resources.markdown import MAX_RESOURCE_BYTES

_BASE = (
    'schema_version = 1\nid = "review"\nname = "Reviewer"\n'
    'description = "Review code"\nwhen_to_use = "Code reviews"\n'
)


class AgentCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f".agent-cards-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.catalog = AgentCatalog(self.root, user_root=self.root / "user")

    def write(self, source: str | None = None, *, user: bool = False) -> Path:
        root = self.root / "user" if user else self.root / ".sec"
        path = root / "agents" / "review" / "card.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            source if source is not None else _BASE + 'instructions = "Review safely."\n',
            encoding="utf-8",
        )
        return path

    def test_discovery_frozen_defaults_and_get(self) -> None:
        self.write()
        cards = self.catalog.reload()
        self.assertEqual(cards, self.catalog.cards)
        card = self.catalog.get("review")
        self.assertIs(card, cards[0])
        self.assertIsNone(self.catalog.get("missing"))
        self.assertEqual(card.capabilities, AgentCapabilities())
        self.assertEqual(card.delegation.allowed_agents, ())
        self.assertEqual(card.delegation.max_depth, 0)
        self.assertEqual(len(card.digest), 64)
        with self.assertRaises(FrozenInstanceError):
            card.name = "Changed"

    def test_explicit_permissions_limits_and_delegation(self) -> None:
        self.write(
            _BASE + 'instructions = "Review."\n'
            '[capabilities]\ntools = ["read"]\nskills = ["review"]\n'
            'mcp_servers = ["git"]\nextensions = ["*"]\n'
            '[limits]\nmax_model_calls = 2\nmax_tool_calls = 4\n'
            '[delegation]\nallowed_agents = ["writer"]\nmax_depth = 1\n'
        )
        card = self.catalog.reload()[0]
        self.assertEqual(card.capabilities.tools, ("read",))
        self.assertEqual(card.capabilities.extensions, ("*",))
        self.assertEqual(card.limits.max_model_calls, 2)
        self.assertEqual(card.limits.max_tool_calls, 4)
        self.assertEqual(card.delegation.allowed_agents, ("writer",))
        self.assertEqual(card.delegation.max_depth, 1)
        self.assertEqual(card.tools, ("read",))
        self.assertEqual(card.max_model_calls, 2)
        self.assertEqual(card.allowed_agents, ("writer",))

    def test_sdk_card_defaults_and_digest(self) -> None:
        card = AgentCard(
            id="default", name="Assistant", description="Default",
            instructions="Help.", tools=("*",), skills=("*",),
            mcp_servers=("*",), extensions=("*",),
        )
        self.assertEqual(card.schema_version, 1)
        self.assertEqual(card.max_model_calls, 8)
        self.assertEqual(card.max_tool_calls, 32)
        self.assertEqual(len(card.digest), 64)

    def test_relative_instructions_and_digest_changes(self) -> None:
        path = self.write(_BASE + 'instructions_file = "rules/review.md"\n')
        instructions = path.parent / "rules" / "review.md"
        instructions.parent.mkdir()
        instructions.write_text("Review safely.", encoding="utf-8")
        before = self.catalog.reload()[0]
        instructions.write_text("Review thoroughly.", encoding="utf-8")
        after = self.catalog.reload()[0]
        self.assertEqual(after.instructions, "Review thoroughly.")
        self.assertNotEqual(before.digest, after.digest)

    def test_workspace_override_is_reported_and_successful(self) -> None:
        user = self.write(_BASE + 'instructions = "User."\n', user=True)
        self.write()
        self.assertEqual(self.catalog.reload()[0].instructions, "Review safely.")
        self.assertEqual(len(self.catalog.issues), 1)
        self.assertEqual(self.catalog.issues[0].path, user.resolve())
        self.assertEqual(self.catalog.issues[0].severity, "notice")

    def test_malformed_schemas_fail_atomically(self) -> None:
        self.write()
        before = self.catalog.reload()
        sources = (
            _BASE.replace("schema_version = 1", "schema_version = true"),
            _BASE.replace("schema_version = 1", "schema_version = 2"),
            _BASE.replace('id = "review"', 'id = "other"'),
            _BASE + 'instructions = "Review."\nunknown = 3\n',
            _BASE + 'instructions = 1\n',
            _BASE + 'instructions = "Review."\ninstructions_file = "rules.md"\n',
            _BASE + 'instructions = "Review."\n[capabilities]\ntools = "*"\n',
            _BASE + 'instructions = "Review."\n[capabilities]\ntools = [1]\n',
            _BASE + 'instructions = "Review."\n[capabilities]\nunknown = []\n',
            _BASE + 'instructions = "Review."\n[limits]\nmax_tool_calls = true\n',
            _BASE + 'instructions = "Review."\n[limits]\nmax_model_calls = 0\n',
            _BASE + 'instructions = "Review."\n[delegation]\nmax_depth = -1\n',
            _BASE + 'instructions = "Review."\n[delegation]\nallowed_agents = [false]\n',
            "[not valid toml",
        )
        for source in sources:
            with self.subTest(source=source):
                self.write(source)
                with self.assertRaises(ValueError):
                    self.catalog.reload()
                self.assertEqual(self.catalog.cards, before)
                self.assertTrue(self.catalog.issues)

    def test_traversal_absolute_and_symlink_escape_rejected(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("External instructions.", encoding="utf-8")
        for relative in ("../../../outside.md", str(outside.resolve())):
            with self.subTest(relative=relative):
                self.write(_BASE + f'instructions_file = "{relative}"\n')
                with self.assertRaisesRegex(ValueError, "relative"):
                    self.catalog.reload()
        path = self.write(_BASE + 'instructions_file = "rules.md"\n')
        (path.parent / "rules.md").symlink_to(outside.resolve())
        with self.assertRaisesRegex(ValueError, "outside"):
            self.catalog.reload()
        path.unlink()
        outside.write_text(_BASE + 'instructions = "External."\n', encoding="utf-8")
        path.symlink_to(outside.resolve())
        with self.assertRaisesRegex(ValueError, "outside"):
            self.catalog.reload()

    def test_card_and_instruction_size_limits(self) -> None:
        path = self.write()
        path.write_bytes(b"x" * (MAX_RESOURCE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.catalog.reload()
        self.write(_BASE + 'instructions_file = "rules.md"\n')
        (path.parent / "rules.md").write_bytes(b"x" * (MAX_RESOURCE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.catalog.reload()

    def test_missing_directories_and_non_directory_root(self) -> None:
        self.assertEqual(self.catalog.reload(), ())
        (self.root / ".sec").mkdir()
        (self.root / ".sec" / "agents").write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "directory"):
            self.catalog.reload()
