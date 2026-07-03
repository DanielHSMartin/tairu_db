# -*- coding: utf-8 -*-

"""Self-check for the generation re-entrancy guard: QgsProject mutations from a
background task must be deferred until the outermost generation finishes (adding a
layer inside a generation's nested event loop crashed QGIS on-device). Pure Python."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tairu_core.reentrancy_guard as guard  # noqa: E402
from tairu_core.reentrancy_guard import enter, leave, active, run_or_defer  # noqa: E402


class TestGuard(unittest.TestCase):
    def setUp(self):
        guard._depth = 0
        guard._pending.clear()

    def test_runs_immediately_when_inactive(self):
        calls = []
        run_or_defer(lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertFalse(active())

    def test_defers_while_active_and_drains_in_order_on_leave(self):
        calls = []
        enter()
        self.assertTrue(active())
        run_or_defer(lambda: calls.append('a'))
        run_or_defer(lambda: calls.append('b'))
        self.assertEqual(calls, [])            # deferred, not run yet
        leave()
        self.assertFalse(active())
        self.assertEqual(calls, ['a', 'b'])    # drained, in order

    def test_nested_generation_drains_only_at_outermost(self):
        calls = []
        enter()
        enter()
        run_or_defer(lambda: calls.append(1))
        leave()
        self.assertEqual(calls, [])            # still one level deep
        leave()
        self.assertEqual(calls, [1])

    def test_a_raising_deferred_fn_does_not_block_the_rest(self):
        calls = []
        enter()

        def boom():
            raise RuntimeError('boom')

        run_or_defer(boom)
        run_or_defer(lambda: calls.append('ok'))
        leave()
        self.assertEqual(calls, ['ok'])

    def test_leave_without_enter_is_safe(self):
        leave()  # never goes negative
        self.assertFalse(active())


if __name__ == '__main__':
    unittest.main()
