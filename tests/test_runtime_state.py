"""验证 sec Run 生命周期状态与合法迁移。"""

from __future__ import annotations

import unittest

from sec._runtime.state import RunState


class RunStateTests(unittest.TestCase):
    """覆盖 Run 状态迁移及终态不可回退约束。"""

    def test_state_tracks_approval_and_terminal_transitions(self) -> None:
        state = RunState()

        self.assertEqual(state.status, "created")
        state.start()
        self.assertEqual(state.status, "running")
        state.wait_for_approval()
        self.assertEqual(state.status, "waiting_approval")
        state.resume_after_approval()
        state.finish("completed")

        self.assertEqual(state.status, "completed")

    def test_invalid_transition_is_rejected(self) -> None:
        state = RunState()

        with self.assertRaisesRegex(RuntimeError, "invalid Run state transition"):
            state.resume_after_approval()

    def test_terminal_state_cannot_be_reentered_or_changed(self) -> None:
        state = RunState()
        state.start()
        state.finish("failed")

        with self.assertRaisesRegex(RuntimeError, "already in a terminal state"):
            state.finish("completed")

    def test_nonterminal_status_is_rejected_as_finish_result(self) -> None:
        state = RunState()

        with self.assertRaisesRegex(ValueError, "terminal status"):
            state.finish("running")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
