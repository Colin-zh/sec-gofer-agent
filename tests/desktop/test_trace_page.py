from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from sec.apps.desktop.trace_page import TracePage, TracePayloadView
from sec.apps.desktop.trace_store import TraceRecorder, TraceStore


class TracePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TraceStore(Path(self.directory.name) / "traces.sqlite3")
        for run_id, session_id, status in (
            ("run-current", "session-a", "running"),
            ("run-history", "session-b", "completed"),
        ):
            recorder = TraceRecorder(self.store, run_id, session_id)
            recorder.finish(status)
        self.page = TracePage(self.store)
        self.addCleanup(self.page.close)

    def test_run_selector_and_narrow_layout_show_trace_details(self) -> None:
        self.assertEqual(
            [run["run_id"] for run in self.store.list_runs(session_id="session-a")],
            ["run-current"],
        )
        self.page.resize(620, 520)
        self.page.show()
        self.page.refresh_runs(
            self.store.list_runs(),
            "run-current",
            session_id="session-a",
        )
        self.app.processEvents()

        self.assertEqual(self.page._runs.count(), 1)
        self.assertEqual(self.page._runs.currentData(), "run-current")
        self.assertIn("#run-curr", self.page._runs.currentText())
        self.assertEqual(
            self.page._runs.currentData(Qt.ItemDataRole.ToolTipRole),
            "Run ID: run-current\nSession ID: session-a",
        )
        self.assertEqual(
            self.page._splitter.orientation(),
            Qt.Orientation.Vertical,
        )
        root = self.page._tree.topLevelItem(0)
        self.assertIsNotNone(root)
        self.assertEqual(root.text(0).strip(), "Run · run-curr")
        self.assertIn("执行耗时", self.page._detail_tabs[0].toPlainText())
        self.assertEqual(self.page._input_view._editor.toPlainText(), "未记录")
        self.assertEqual(self.page._tabs.tabText(0), "预览")

        self.page.refresh_runs(
            self.store.list_runs(),
            session_id="session-b",
        )
        self.assertEqual(self.page._runs.count(), 1)
        self.assertEqual(self.page._selected_run_id, "run-history")

    def test_trace_controls_use_desktop_palette_without_back_button(self) -> None:
        self.assertFalse(any(
            button.text() == "返回对话" for button in self.page.findChildren(QPushButton)
        ))
        style = self.page.styleSheet()
        self.assertIn("background: #dfe7f2", style)
        self.assertIn("border-radius: 10px", style)
        self.assertNotIn("#4f6bed", style)
        self.assertNotIn("#f0fdf6", style)
        for combo in (
            self.page._runs, self.page._input_view._format, self.page._output_view._format,
        ):
            self.assertIn("border-radius: 8px", combo.styleSheet())
            self.assertIn("background: #edf2fa", combo.view().styleSheet())

    def _add_tool(self) -> tuple[str, str]:
        turn = self.store.create_node(
            "run-current", "run-current", "turn", "轮次 1", 100,
        )
        tool = self.store.create_node(
            "run-current", turn, "tool", "工具 · read_file", 101,
            details={
                "input": {"path": "src/main.py", "api_key": "not-for-display"},
                "partial_output": {"lines": ["first", "second"]},
            },
        )
        self.store.update_node(tool, elapsed_ms=1299, status="completed")
        self.store.record_event(
            "run-current", tool, "tool_execution_end", 102, 2000,
            {"output": {"lines": ["first", "second"]}},
        )
        self.page.refresh_runs(self.store.list_runs(), "run-current")
        return turn, tool

    def test_tool_preview_metadata_events_and_raw_json_use_stored_values(self) -> None:
        _turn, tool = self._add_tool()
        item = self.page._find_tree_item(tool)
        self.page._tree.setCurrentItem(item)

        self.assertEqual(self.page._node_title.text(), "工具 · read_file")
        self.assertIn("session-a", self.page._metadata.text())
        self.assertIn("1.30 s", self.page._metadata.text())
        self.assertEqual(item.text(1), "1.30 s")
        self.assertEqual(item.text(2), "已完成")
        self.assertEqual(item.toolTip(2), "completed")
        self.assertFalse(item.icon(0).isNull())
        self.assertEqual(
            json.loads(self.page._input_view._editor.toPlainText()),
            {"path": "src/main.py", "api_key": "[REDACTED]"},
        )
        self.assertEqual(
            json.loads(self.page._output_view._editor.toPlainText()),
            {"lines": ["first", "second"]},
        )
        self.assertIn("tool_execution_end", self.page._detail_tabs[1].toPlainText())
        raw = json.loads(self.page._detail_tabs[2].toPlainText())
        self.assertEqual(raw["node"]["node_id"], tool)
        self.assertEqual(len(raw["events"]), 1)
        self.assertNotIn("children", raw["node"])
        self.assertNotIn("not-for-display", str(raw))

    def test_live_refresh_preserves_selection_expansion_format_and_tab(self) -> None:
        turn, tool = self._add_tool()
        self.page._tree.setCurrentItem(self.page._find_tree_item(tool))
        self.page._find_tree_item(turn).setExpanded(False)
        self.page._tabs.setCurrentIndex(2)
        self.page._input_view._format.setCurrentIndex(1)
        self.page._output_view._tree.topLevelItem(0).setExpanded(False)
        self.store.update_node(tool, details={"output": {"answer": "done"}})
        self.page.refresh_run("run-current")

        self.assertEqual(self.page._selected_node_id(), tool)
        self.assertFalse(self.page._find_tree_item(turn).isExpanded())
        self.assertEqual(self.page._tabs.currentIndex(), 2)
        self.assertEqual(self.page._input_view._stack.currentIndex(), 1)
        self.assertEqual(
            json.loads(self.page._output_view._editor.toPlainText()),
            {"answer": "done"},
        )
        self.page._output_view._tree.topLevelItem(0).setExpanded(False)
        self.page.refresh_runs(self.store.list_runs())
        self.assertFalse(self.page._output_view._tree.topLevelItem(0).isExpanded())
        self.assertEqual(self.page._selected_node_id(), tool)
        self.page._search.setText("read_file")
        self.page._find_tree_item(turn).setExpanded(False)
        self.page.refresh_run("run-current")
        self.assertFalse(self.page._find_tree_item(turn).isExpanded())
        self.assertEqual(self.page._search.text(), "read_file")

    def test_search_keeps_ancestors_and_navigation_follows_visible_nodes(self) -> None:
        turn, tool = self._add_tool()
        other = self.store.create_node(
            "run-current", turn, "model", "模型 · example-model", 102,
        )
        self.page.refresh_run("run-current")
        self.page._search.setText("READ_FILE")
        self.assertFalse(self.page._find_tree_item(turn).isHidden())
        self.assertFalse(self.page._find_tree_item(tool).isHidden())
        self.assertTrue(self.page._find_tree_item(other).isHidden())
        self.page._next.click()
        self.assertEqual(self.page._selected_node_id(), turn)
        self.page._next.click()
        self.assertEqual(self.page._selected_node_id(), tool)
        self.assertFalse(self.page._next.isEnabled())
        self.page._previous.click()
        self.assertEqual(self.page._selected_node_id(), turn)
        self.page._search.setText("no matching node")
        self.assertEqual(self.page._tree_hint.text(), "没有匹配的节点")
        self.assertFalse(self.page._next.isEnabled())
        self.page._search.clear()
        self.assertFalse(self.page._find_tree_item(other).isHidden())
        self.page._tree.setCurrentItem(self.page._tree.topLevelItem(0))
        self.page._collapse.click()
        self.assertFalse(self.page._next.isEnabled())
        self.page._expand.click()
        self.assertTrue(self.page._next.isEnabled())
        self.page._search.setText("已完成")
        self.assertFalse(self.page._find_tree_item(tool).isHidden())
        self.assertTrue(self.page._find_tree_item(other).isHidden())

    def test_empty_runs_clear_previous_details_and_wide_layout_is_horizontal(self) -> None:
        self._add_tool()
        self.page.resize(1050, 720)
        self.page.show()
        self.app.processEvents()
        self.assertEqual(self.page._splitter.orientation(), Qt.Orientation.Horizontal)
        self.page.refresh_runs([])
        self.assertIsNone(self.page._selected_run_id)
        self.assertEqual(self.page._tree.topLevelItemCount(), 0)
        self.assertEqual(self.page._metadata.text(), "")
        self.assertEqual(self.page._input_view._editor.toPlainText(), "未记录")
        self.assertFalse(self.page._next.isEnabled())
        self.assertEqual(self.page._detail_tabs[2].toPlainText(), "未记录")

    def test_payload_formats_copy_and_empty_values(self) -> None:
        view = TracePayloadView("测试")
        self.addCleanup(view.close)
        for value in ({}, [], False, 0, "", None, {"nested": [True, None]}):
            with self.subTest(value=value):
                view.set_value(value)
                structured = isinstance(value, (dict, list))
                self.assertEqual(view._format.isEnabled(), structured)
                expected = value if isinstance(value, str) else (
                    "未记录" if value is None else json.dumps(value, ensure_ascii=False, indent=2)
                )
                self.assertEqual(view._editor.toPlainText(), expected)
                view._copy.click()
                self.assertEqual(self.app.clipboard().text(), expected)
        view._format.setCurrentIndex(1)
        self.assertIs(view._stack.currentWidget(), view._editor)
        view._format.setCurrentIndex(0)
        self.assertIs(view._stack.currentWidget(), view._tree)
        root = view._tree.topLevelItem(0)
        self.assertEqual(root.child(0).text(0), "nested")
        self.assertEqual(root.child(0).childCount(), 2)


if __name__ == "__main__":
    unittest.main()
