"""Synchronous APIs use the same workspace runtime and resource lifecycle."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sec import SecClient
from tests.test_capability_runtime import ScriptedProvider, card, response, skill


class SyncCapabilitiesTests(unittest.TestCase):
    def test_workspace_selection_reload_and_activation(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as home:
            root = Path(directory)
            card(root, "writer")
            skill(root)
            provider = ScriptedProvider([response()])
            with patch("pathlib.Path.home", return_value=Path(home)), patch(
                "sec.apis.client.create_provider", return_value=provider,
            ):
                with SecClient(
                    workspace=root, provider="openai", model="test", api_key="test",
                    env_file=None, session_store_backend="in_memory",
                ) as active:
                    session_id = active.session.id
                    active.select_agent("writer")
                    self.assertEqual(active.selected_agent_id, "writer")
                    report = active.set_resource_enabled("skill", "inspect", False)
                    self.assertIn("skill/inspect", report.removed)
                    self.assertEqual(active.resources.snapshot.skills, ())
                    active.set_resource_enabled("skill", "inspect", True)
                    result = active.run("hello")
                    self.assertEqual(result.text, "done")
                    self.assertEqual(active.session.id, session_id)
                    before = active.capability_generation
                    active.reload_resources()
                    self.assertGreater(active.capability_generation, before)
