import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.controllers import distance_self_check as module


class DecisionWindowTest(unittest.TestCase):
    def test_missing_plate_suspects_then_revokes_or_confirms_dynamically(self):
        class FakeHandoff:
            def __init__(self):
                self.events = []

            def suspect_local_removal(self, state):
                self.events.append(("suspected", int(state["pass_id"])))

            def revoke_local_removal(self, state):
                self.events.append(("revoked", int(state["pass_id"])))

            def publish_removed_plate(self, state):
                self.events.append(("confirmed", int(state["pass_id"])))

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(module, "OUTPUT_ROOT", Path(temporary_directory)):
                handoff = FakeHandoff()
                recorder = module.SelfCheckRecorder(
                    handoff=handoff, continuous_mode=True
                )
        state = recorder._new_pass(1, 30.0, 10.0, 1)
        state["handoff_detected"] = True
        state["last_seen"] = 10.0

        recorder._finalize_missing_passes(set(), 10.4, 2)
        self.assertEqual(handoff.events, [("suspected", 1)])
        self.assertNotIn(state["stage"], {"caught", "exited"})

        recorder._assign_pass(2, 34.0, 10.5, 3, set())
        self.assertEqual(handoff.events[-1], ("revoked", 1))

        state["last_seen"] = 20.0
        state["missing_since"] = None
        recorder._finalize_missing_passes(set(), 20.4, 4)
        recorder._finalize_missing_passes(set(), 21.6, 5)
        self.assertEqual(handoff.events[-2:], [
            ("suspected", 1), ("confirmed", 1)
        ])
        self.assertEqual(state["stage"], "caught")

    def test_gray_is_preserved_as_raw_evidence_but_operationally_green(self):
        instance = {"current_color": "gray", "stable_color": "gray"}
        module.normalize_operational_colors(instance)
        self.assertEqual(instance["raw_current_color"], "gray")
        self.assertEqual(instance["raw_stable_color"], "gray")
        self.assertEqual(instance["current_color"], "light_green")
        self.assertEqual(instance["stable_color"], "light_green")

        known = {"current_color": "orange", "stable_color": "light_green"}
        module.normalize_operational_colors(known)
        self.assertEqual(known["current_color"], "orange")
        self.assertEqual(known["stable_color"], "light_green")

    def test_continuous_mode_auto_records_and_rejects_exit_only_fragment(self):
        class FakeHandoff:
            def __init__(self):
                self.detected = []
                self.finalized = []
                self.removed = []

            def publish_locked_plate(self, state):
                self.finalized.append(int(state["pass_id"]))

            def publish_detected_plate(self, state, color, confidence, observations):
                state["handoff_detected"] = True
                state["handoff_sequence"] = len(self.detected) + 1
                self.detected.append((int(state["pass_id"]), color, observations))
                return True

            def publish_final_plate(self, state):
                state["handoff_final"] = True
                self.finalized.append(int(state["pass_id"]))

            def publish_removed_plate(self, state):
                self.removed.append(int(state["pass_id"]))

            def finalize_downstream(self, _state):
                pass

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(module, "OUTPUT_ROOT", Path(temporary_directory)):
                handoff = FakeHandoff()
                recorder = module.SelfCheckRecorder(
                    handoff=handoff, continuous_mode=True
                )
            recorder._record_color_calibration = lambda *_args, **_kwargs: None
            self.assertTrue(recorder.evaluation_active)
            self.assertEqual(recorder.current_lap_id, 1)

            for index, distance in enumerate((6.0, 25.0, 50.0, 66.0), start=1):
                recorder.record(index, [{
                    "tracker_id": 1, "distance_cm": distance,
                    "confidence": 0.9, "stable_color": "orange",
                }], np.zeros((2, 2, 3), np.uint8))
            self.assertEqual(handoff.detected, [(1, "orange", 3)])
            self.assertEqual(handoff.finalized, [1])
            self.assertEqual(recorder.pass_states[1]["expected_color"], "")
            recorder.save()
            self.assertTrue(
                (recorder.directory / "downstream_color_events.csv").exists()
            )
            self.assertTrue(
                (recorder.directory / "downstream_color_summary.csv").exists()
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(module, "OUTPUT_ROOT", Path(temporary_directory)):
                handoff = FakeHandoff()
                recorder = module.SelfCheckRecorder(
                    handoff=handoff, continuous_mode=True
                )
            recorder._record_color_calibration = lambda *_args, **_kwargs: None
            recorder.record(1, [{
                "tracker_id": 9, "distance_cm": 64.0,
                "confidence": 0.9, "stable_color": "orange",
            }], np.zeros((2, 2, 3), np.uint8))
            recorder.record(2, [{
                "tracker_id": 9, "distance_cm": 66.0,
                "confidence": 0.9, "stable_color": "orange",
            }], np.zeros((2, 2, 3), np.uint8))
            self.assertEqual(handoff.detected, [])
            self.assertEqual(handoff.finalized, [])
            self.assertTrue(recorder.pass_states[1]["publish_rejected"])

    def test_reconnect_uses_bbox_gate_and_blocks_duplicate_tracker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(module, "OUTPUT_ROOT", Path(temporary_directory)):
                recorder = module.SelfCheckRecorder()
            recorder.start_color_cycle()
            first = recorder._assign_pass(
                10, 10.0, 1.0, 1, set(), box=(0, 0, 40, 40)
            )
            self.assertEqual(first["pass_id"], 1)
            assigned = set()
            reconnected = recorder._assign_pass(
                11, 12.0, 1.2, 2, assigned, box=(5, 5, 45, 45)
            )
            self.assertEqual(reconnected["pass_id"], 1)
            duplicate = recorder._assign_pass(
                11, 12.5, 1.2, 2, assigned, box=(6, 5, 46, 45)
            )
            self.assertIsNone(duplicate)
            different_plate = recorder._assign_pass(
                12, 13.0, 1.3, 3, set(), box=(300, 300, 340, 340)
            )
            self.assertEqual(different_plate["pass_id"], 2)

    def test_reconnect_keeps_close_green_yellow_plates_in_belt_order(self):
        """A fragmented leading plate must not trade IDs with its follower."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(module, "OUTPUT_ROOT", Path(temporary_directory)):
                recorder = module.SelfCheckRecorder(continuous_mode=True)
        recorder._record_color_calibration = lambda *_args, **_kwargs: None
        image = np.zeros((2, 2, 3), np.uint8)

        # The leading green plate has enough local evidence, then ByteTrack
        # fragments it.  Its replacement box is intentionally far away to
        # reproduce the bbox gate that previously created a new pass.
        recorder.record(1, [{
            "tracker_id": 6, "distance_cm": 6.0, "confidence": 0.98,
            "stable_color": "light_green", "xyxy": (0, 0, 40, 40),
        }], image)
        recorder.record(2, [{
            "tracker_id": 7, "distance_cm": 10.0, "confidence": 0.98,
            "stable_color": "light_green", "xyxy": (4, 0, 44, 40),
        }], image)
        recorder.record(3, [
            {
                "tracker_id": 11, "distance_cm": 19.0, "confidence": 0.98,
                "stable_color": "light_green", "xyxy": (250, 0, 290, 40),
            },
            {
                "tracker_id": 10, "distance_cm": 6.5, "confidence": 0.98,
                "stable_color": "yellow", "xyxy": (0, 100, 40, 140),
            },
        ], image)

        self.assertEqual(recorder.tracker_to_pass[11], 1)
        self.assertEqual(recorder.tracker_to_pass[10], 2)
        self.assertEqual(
            recorder._stable_local_color(recorder.pass_states[1]),
            "light_green",
        )
        self.assertEqual(
            recorder._stable_local_color(recorder.pass_states[2]),
            None,
        )

    def test_buffers_do_not_score_and_exit_locks_result(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(module, "OUTPUT_ROOT", Path(temporary_directory)):
                recorder = module.SelfCheckRecorder()
            recorder.start_color_cycle()
            recorder._record_color_calibration = lambda *_args, **_kwargs: None

            def observe(index, distance, color):
                instance = {
                    "tracker_id": 1,
                    "distance_cm": float(distance),
                    "confidence": 0.9,
                    "stable_color": color,
                }
                recorder.record(index, [instance], np.zeros((2, 2, 3), np.uint8))
                return instance

            entry = observe(1, module.DETECTION_START_CM - 3.0, "yellow")
            observe(2, module.DETECTION_START_CM, "orange")
            observe(3, module.DETECTION_START_CM + 10.0, "orange")
            observe(4, module.DETECTION_END_CM - 1.0, "orange")
            exit_first = observe(5, module.DETECTION_END_CM + 1.0, "light_green")
            exit_later = observe(6, module.TRACK_LENGTH_CM - 1.0, "yellow")

            self.assertEqual(entry["measurement_phase"], "entry_buffer")
            self.assertEqual(exit_first["measurement_phase"], "exit_buffer")
            self.assertEqual(exit_later["measurement_phase"], "exit_buffer")
            self.assertEqual(
                [row["distance_cm"] for row in recorder.rows],
                [module.DETECTION_START_CM, module.DETECTION_START_CM + 10.0,
                 module.DETECTION_END_CM - 1.0],
            )
            self.assertTrue(all(
                module.DETECTION_START_CM <= row["distance_cm"] < module.DETECTION_END_CM
                for row in recorder.rows
            ))
            state = recorder.pass_states[1]
            self.assertTrue(state["decision_locked"])
            self.assertEqual(state["final_color"], "orange")
            self.assertEqual(state["final_observations"], 3)
            state["last_seen"] -= module.PASS_RECONNECT_MAX_SECONDS + 0.1
            recorder.record(7, [], np.zeros((2, 2, 3), np.uint8))
            self.assertEqual(state["stage"], "exited")

            metrics = recorder.metrics()
            self.assertEqual(metrics[0]["distance_start_cm"], module.DETECTION_START_CM)
            self.assertEqual(metrics[-1]["distance_end_cm"], module.DETECTION_END_CM)
            self.assertEqual(
                len(metrics), int(module.REQUIRED_DETECTION_LENGTH_CM / module.BIN_SIZE_CM)
            )
            self.assertEqual(metrics[0]["plate_id_continuity"], 1)
            self.assertEqual(metrics[0]["plate_color_consistency"], 1.0)
            self.assertEqual(metrics[-1]["continuous_range_qualified"], 1)
            report = recorder.analysis_markdown(metrics, recorder.pass_bin_results())
            self.assertIn(f"{module.DETECTION_END_CM:g} cm", report)

            original_rows = list(recorder.rows)
            original_pass_ids = set(recorder.pass_states)
            next_pass_id = recorder.next_pass_id
            recorder.start_color_cycle()
            self.assertEqual(recorder.current_lap_id, 2)
            self.assertEqual(recorder.rows, original_rows)
            self.assertEqual(set(recorder.pass_states), original_pass_ids)
            self.assertEqual(recorder.next_pass_id, next_pass_id)
            self.assertEqual(recorder.tracker_to_pass, {})


if __name__ == "__main__":
    unittest.main()
