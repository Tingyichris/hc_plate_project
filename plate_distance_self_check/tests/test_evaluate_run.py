import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.evaluation import align_sequence, parse_sequence, sequence_result


class EvaluateRunTest(unittest.TestCase):
    def test_sequence_alignment_reports_missing_green_and_final_orange(self):
        truth = parse_sequence("GGOYGOOO")
        result = sequence_result(
            truth, parse_sequence("GGOYOO")
        )
        self.assertEqual(result["exact_color_matches"], 6)
        self.assertEqual(result["missed"], 2)
        self.assertEqual(result["extra"], 0)
        self.assertEqual(result["wrong_color"], 0)

    def test_alignment_is_deterministic(self):
        first = align_sequence(parse_sequence("GGO"), parse_sequence("GO"))
        second = align_sequence(parse_sequence("GGO"), parse_sequence("GO"))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
