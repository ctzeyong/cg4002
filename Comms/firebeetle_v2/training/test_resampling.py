"""Checks for 500 Hz selection, especially gaps hidden between selected readings."""
import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import select_500hz


class SelectionTests(unittest.TestCase):
    def select(self, seq, ignore=None):
        seq = np.asarray(seq, dtype='int64')
        return select_500hz(seq, 123456789+seq*2000, np.ones(len(seq), dtype=bool),
                            np.zeros(len(seq), dtype=bool) if ignore is None else ignore)

    def test_fifth_sample_and_partial_tail(self):
        idx, valid, ignored = self.select(np.arange(103, 120))
        np.testing.assert_array_equal(idx, [4, 9, 14])
        self.assertTrue(valid.all())
        self.assertFalse(ignored.any())

    def test_gap_in_unselected_reading_invalidates_next_group(self):
        seq = np.delete(np.arange(20), 6)
        idx, valid, _ = self.select(seq)
        np.testing.assert_array_equal(seq[idx], [4, 9, 14, 19])
        np.testing.assert_array_equal(valid, [True, False, True, True])

    def test_missing_selected_reading_preserves_grid_gap(self):
        seq = np.delete(np.arange(20), 9)
        idx, valid, _ = self.select(seq)
        np.testing.assert_array_equal(seq[idx], [4, 14, 19])
        np.testing.assert_array_equal(valid, [True, False, True])

    def test_ignore_between_selected_readings_is_not_lost(self):
        ignored = np.zeros(15, dtype=bool)
        ignored[7] = True
        _, valid, skip = self.select(np.arange(15), ignored)
        self.assertTrue(valid.all())
        np.testing.assert_array_equal(skip, [False, True, False])

    def test_too_short(self):
        idx, valid, skip = self.select(np.arange(4))
        self.assertEqual((len(idx), len(valid), len(skip)), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
