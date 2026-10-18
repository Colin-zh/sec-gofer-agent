"""验证可移植 Session 文件格式及安全创建行为。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sec._types.content import TextContent
from sec._types.messages import UserMessage
from sec._types.session import MessageEntry, SessionSnapshot
from sec.apps.cli.session_io import (
    MAX_SESSION_FILE_BYTES,
    decode_session,
    encode_session,
    read_session_file,
    write_session_file,
)


class SessionIOTests(unittest.TestCase):
    """覆盖 Session 导入导出的版本与文件边界。"""

    def test_portable_format_round_trips_branch_tree(self) -> None:
        entries = (
            MessageEntry("root", None, 1.0, UserMessage([TextContent("root")], 1.0)),
            MessageEntry(
                "left",
                "root",
                2.0,
                UserMessage([TextContent("left")], 2.0),
            ),
            MessageEntry(
                "right",
                "root",
                3.0,
                UserMessage([TextContent("right")], 3.0),
            ),
        )
        snapshot = SessionSnapshot("source-session", 4, entries, "right")

        restored = decode_session(encode_session(snapshot).encode("utf-8"))

        self.assertEqual(restored.session_id, "source-session")
        self.assertEqual(restored.version, 0)
        self.assertEqual(restored.entries, entries)
        self.assertEqual(restored.active_leaf_id, "right")
        self.assertEqual([message.text for message in restored.messages], ["root", "right"])

    def test_export_is_exclusive_and_import_reads_valid_file(self) -> None:
        snapshot = SessionSnapshot(
            "session",
            0,
            (MessageEntry("entry", None, 1.0, UserMessage("hello", 1.0)),),
            "entry",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"

            self.assertEqual(write_session_file(snapshot, path), path)
            self.assertEqual(read_session_file(path).entries, snapshot.entries)
            with self.assertRaises(FileExistsError):
                write_session_file(snapshot, path)

    def test_import_rejects_invalid_format_and_oversized_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "format"):
            decode_session(b'{"format":"unknown"}')
        with self.assertRaisesRegex(ValueError, "exceeds"):
            decode_session(b"x" * (MAX_SESSION_FILE_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
