"""Persistent worker configuration does not import or execute extensions."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sec._runtime.resource_settings import (
    read_worker_extensions, validate_extension_names, write_worker_extensions,
)


class ResourceSettingsTests(unittest.TestCase):
    def test_absent_configuration_is_empty_and_roundtrip_is_exact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(read_worker_extensions(root), ())
            write_worker_extensions(root, ("one", "two-tools"))
            self.assertEqual(read_worker_extensions(root), ("one", "two-tools"))
            write_worker_extensions(root, ())
            self.assertEqual(read_worker_extensions(root), ())
            self.assertEqual(
                tuple((root / ".sec").iterdir()),
                (root / ".sec" / "worker-extensions.json",),
            )

    def test_malformed_configuration_is_not_treated_as_empty(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".sec" / "worker-extensions.json"
            path.parent.mkdir()
            for source in ('not-json', '{"enabled":"plugin"}', '{"enabled":[],"extra":true}'):
                path.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_worker_extensions(root)

    def test_duplicate_invalid_or_nonstring_names_are_rejected(self):
        for names in (("one", "one"), ("../one",), ("",), (1,), "one"):
            with self.subTest(names=names), self.assertRaises(ValueError):
                validate_extension_names(names)
