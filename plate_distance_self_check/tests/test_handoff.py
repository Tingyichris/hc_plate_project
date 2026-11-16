import json
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.handoff import HandoffMQTT, align_monotonic_events


class HandoffTest(unittest.TestCase):
    def test_hc_event_contract_and_topic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="upstream", camera_id="cam-2", peer_camera_id="cam-3",
                output_directory=Path(temporary_directory),
            )
            published = []

            class Result:
                rc = 0

            def capture(topic, payload, qos, retain):
                published.append((topic, json.loads(payload), qos, retain))
                return Result()

            handoff.client.publish = capture
            self.assertTrue(handoff._publish_hc_event(
                "entry", 7, "light_green", 3
            ))
            topic, payload, qos, retain = published[0]
            self.assertEqual(topic, "sushi/events/cam-2")
            self.assertEqual(payload["event"], "entry")
            self.assertEqual(payload["plate_id"], 7)
            self.assertEqual(payload["class"], "light_green")
            self.assertEqual(payload["color"], "#88d66c")
            self.assertEqual(payload["local_pass_id"], 3)
            self.assertEqual(qos, 1)
            self.assertFalse(retain)

    def test_upstream_hc_lifecycle_is_entry_exit_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="upstream", camera_id="cam-2", peer_camera_id="cam-3",
                output_directory=Path(temporary_directory),
            )
            handoff._publish = lambda *_args, **_kwargs: True
            hc_events = []
            handoff._publish_hc_event = (
                lambda event, plate_id, color, local_pass_id, extra=None:
                hc_events.append((event, plate_id, color, local_pass_id)) or True
            )
            state = {"pass_id": 4, "lap_id": 1}
            handoff.publish_detected_plate(state, "orange", 0.9, 3)
            handoff.publish_detected_plate(state, "orange", 0.9, 4)
            state.update({
                "final_color": "orange", "final_confidence_median": 0.95,
                "final_observations": 20,
            })
            handoff.publish_final_plate(state)
            handoff.publish_final_plate(state)
            self.assertEqual(
                [item[0] for item in hc_events], ["entry", "exit"]
            )
            self.assertEqual(hc_events[0][1], hc_events[1][1])

    def test_downstream_hc_lifecycle_is_entry_catch_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="downstream", camera_id="cam-3", peer_camera_id="cam-2",
                output_directory=Path(temporary_directory),
            )
            handoff._publish = lambda *_args, **_kwargs: True
            hc_events = []
            handoff._publish_hc_event = (
                lambda event, plate_id, color, local_pass_id, extra=None:
                hc_events.append((event, plate_id, color, local_pass_id)) or True
            )
            state = {
                "pass_id": 5, "lap_id": 1, "first_seen_unix": time.time(),
                "local_color": "yellow", "local_color_votes": {"yellow": 4},
                "tracker_ids": {9},
            }
            handoff.match_downstream_entry(state)
            handoff.match_downstream_entry(state)
            self.assertEqual(hc_events, [])
            handoff.publish_downstream_entry(state, "yellow")
            handoff.publish_downstream_entry(state, "yellow")
            handoff.finalize_downstream_removed(state)
            handoff.finalize_downstream_removed(state)
            self.assertEqual(
                [item[0] for item in hc_events], ["entry", "catch"]
            )

    def test_global_alignment_recovers_after_missing_middle_plate(self):
        upstream = [
            {"sequence": 1, "detected_at": 0.0},
            {"sequence": 2, "detected_at": 2.0},
            {"sequence": 3, "detected_at": 4.0},
        ]
        downstream = [
            {"pass_id": 1, "first_seen_at": 3.8},
            {"pass_id": 2, "first_seen_at": 7.8},
        ]
        operations = align_monotonic_events(upstream, downstream)
        self.assertEqual(
            [item["operation"] for item in operations],
            ["match", "skip_upstream", "match"],
        )
        self.assertEqual(operations[0]["upstream"]["sequence"], 1)
        self.assertEqual(operations[2]["upstream"]["sequence"], 3)

    def test_live_dp_realigns_without_consuming_fifo(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="downstream", camera_id="cam-2", peer_camera_id="cam-3",
                output_directory=Path(temporary_directory), matching_mode="dp",
            )
            for sequence, detected_at in ((1, 100.0), (2, 102.0), (3, 104.0)):
                entry = {
                    "session_id": "cam-3-dp", "upstream_pass_id": sequence,
                    "lap_id": 1, "sequence": sequence, "color": "orange",
                    "timestamp": detected_at, "received_at": detected_at,
                    "final_received": True,
                }
                handoff._upstream_lifecycle[("cam-3-dp", sequence)] = entry
                handoff._pending.append(entry)

            first = {"pass_id": 1, "lap_id": 1, "first_seen_unix": 103.8}
            second = {"pass_id": 2, "lap_id": 1, "first_seen_unix": 107.8}
            handoff.match_downstream_entry(first)
            handoff.match_downstream_entry(second)

            self.assertEqual(handoff.downstream_binding(1)["sequence"], 1)
            self.assertEqual(handoff.downstream_binding(2)["sequence"], 3)
            self.assertEqual(len(handoff._pending), 3)
            selected = [
                item for item in handoff._alignment_candidates if item["selected"]
            ]
            self.assertEqual(
                [(item["source_sequence"], item["target_local_pass_id"])
                 for item in selected],
                [(1, 1), (3, 2)],
            )

    def test_live_snapshot_does_not_finalize_pending_plate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="downstream", camera_id="cam-2", peer_camera_id="cam-3",
                output_directory=Path(temporary_directory),
            )
            handoff._pending.append({
                "session_id": "cam-3-live", "upstream_pass_id": 1,
                "lap_id": 1, "sequence": 1, "color": "orange",
                "timestamp": time.time(), "received_at": time.time(),
                "final_received": True,
            })
            handoff.snapshot()
            self.assertEqual(len(handoff._pending), 1)
            self.assertEqual(handoff.rows, [])

    def test_downstream_claims_detected_id_and_accepts_final_update(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="downstream", camera_id="cam-2", peer_camera_id="cam-3",
                output_directory=Path(temporary_directory),
            )
            class Message:
                def __init__(self, payload):
                    self.payload = json.dumps(payload).encode("utf-8")

            base = {
                "schema": 1, "camera_id": "cam-3", "role": "upstream",
                "session_id": "cam-3-test", "upstream_pass_id": 8,
                "lap_id": 1, "sequence": 2,
                "timestamp": time.time() - 3.8,
            }
            handoff._on_message(None, None, Message({
                **base, "message_id": "detected", "kind": "plate_detected",
                "color": "orange", "confidence": 0.8, "observations": 3,
            }))
            state = {"pass_id": 5, "lap_id": 1, "first_seen_unix": time.time()}
            self.assertEqual(handoff.match_downstream_entry(state), "orange")
            handoff._on_message(None, None, Message({
                **base, "message_id": "final", "kind": "plate_final",
                "timestamp": time.time(), "color": "yellow",
                "confidence": 0.95, "observations": 20,
            }))
            self.assertEqual(handoff.downstream_binding(5)["color"], "yellow")
            self.assertTrue(handoff.downstream_binding(5)["final_received"])

    def test_upstream_lifecycle_keeps_one_id(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="upstream", camera_id="cam-3", peer_camera_id="cam-2",
                output_directory=Path(temporary_directory),
            )
            published = []
            handoff._publish = lambda kind, body: published.append((kind, body)) or True
            state = {"pass_id": 3, "lap_id": 1}
            handoff.publish_detected_plate(state, "orange", 0.9, 3)
            state.update({
                "final_color": "orange", "final_confidence_median": 0.95,
                "final_observations": 20,
            })
            handoff.publish_final_plate(state)
            self.assertEqual([item[0] for item in published], [
                "plate_detected", "plate_final"
            ])
            self.assertEqual(
                published[0][1]["upstream_pass_id"],
                published[1][1]["upstream_pass_id"],
            )
            self.assertEqual(
                published[0][1]["sequence"], published[1][1]["sequence"]
            )

    def test_downstream_matches_fifo_before_comparing_color(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="downstream",
                camera_id="cam-2",
                peer_camera_id="cam-3",
                output_directory=Path(temporary_directory),
            )
            # Deliberately use a color different from the downstream result.
            # FIFO must still match it; color is only scored afterwards.
            handoff._pending.append({
                "session_id": "cam-3-test",
                "upstream_pass_id": 7,
                "lap_id": 2,
                "sequence": 1,
                "color": "orange",
                "confidence": 0.91,
                "timestamp": time.time() - 3.0,
            })
            state = {
                "pass_id": 4,
                "lap_id": 2,
                "tracker_ids": {10, 12},
                "final_color": "yellow",
                "final_confidence_median": 0.88,
            }
            self.assertEqual(handoff.match_downstream_entry(state), "orange")
            handoff.finalize_downstream(state)
            self.assertEqual(handoff.rows[0]["upstream_pass_id"], 7)
            self.assertEqual(handoff.rows[0]["downstream_pass_id"], 4)
            self.assertEqual(handoff.rows[0]["color_correct"], 0)
            handoff.save()
            summary = json.loads(
                (Path(temporary_directory) / "handoff_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["fifo_paired"], 1)
            self.assertEqual(
                summary["color_agreement_rate_among_fifo_pairs"], 0.0
            )
            self.assertIsNone(summary["identity_accuracy"])
            self.assertFalse(
                (Path(temporary_directory) / "sequence_alignment.json").exists()
            )
            saved = (
                Path(temporary_directory) / "handoff_results.csv"
            ).read_text(encoding="utf-8-sig")
            self.assertIn("pairing_method", saved)
            self.assertIn("identity_status", saved)
            self.assertIn("fifo_paired", saved)
            self.assertIn("unverified", saved)

    def test_removed_plate_times_out_and_next_plate_realigns(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="downstream",
                camera_id="cam-2",
                peer_camera_id="cam-3",
                output_directory=Path(temporary_directory),
            )
            handoff._publish = lambda *_args, **_kwargs: True
            now = time.time()
            removed = {
                "session_id": "cam-3-pickup",
                "upstream_pass_id": 20,
                "lap_id": 1,
                "sequence": 5,
                "color": "yellow",
                "confidence": 0.97,
                "timestamp": now - 7.0,
                "received_at": now - 7.0,
                "final_received": True,
            }
            following = {
                "session_id": "cam-3-pickup",
                "upstream_pass_id": 21,
                "lap_id": 1,
                "sequence": 6,
                "color": "orange",
                "confidence": 0.98,
                "timestamp": now - 3.8,
                "received_at": now - 3.8,
                "final_received": True,
            }
            handoff._pending.extend([removed, following])

            state = {
                "pass_id": 9,
                "lap_id": 1,
                "first_seen_unix": now,
                "tracker_ids": {30},
                "final_color": "orange",
                "final_confidence_median": 0.96,
            }
            self.assertEqual(handoff.match_downstream_entry(state), "orange")
            self.assertEqual(handoff.downstream_binding(9)["upstream_pass_id"], 21)
            handoff.finalize_downstream(state)

            pickup = [
                row for row in handoff.rows
                if row["handoff_status"] == "removed_between_cameras"
            ]
            self.assertEqual(len(pickup), 1)
            self.assertEqual(pickup[0]["upstream_pass_id"], 20)
            self.assertEqual(pickup[0]["upstream_color"], "yellow")
            matched = [
                row for row in handoff.rows
                if row["handoff_status"] == "fifo_paired"
            ]
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0]["upstream_pass_id"], 21)

            handoff.save()
            summary = json.loads(
                (Path(temporary_directory) / "handoff_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["removed_between_cameras"], 1)
            self.assertEqual(summary["removed_color_counts"], {"yellow": 1})
            self.assertEqual(summary["removed_sequences"], [5])

    def test_upstream_and_downstream_removals_are_separate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = HandoffMQTT(
                role="downstream", camera_id="cam-2", peer_camera_id="cam-3",
                output_directory=Path(temporary_directory),
            )
            handoff._publish = lambda *_args, **_kwargs: True

            class Message:
                def __init__(self, payload):
                    self.payload = json.dumps(payload).encode("utf-8")

            base = {
                "schema": 1, "camera_id": "cam-3", "role": "upstream",
                "session_id": "cam-3-removal", "lap_id": 1,
                "timestamp": time.time(),
            }
            handoff._on_message(None, None, Message({
                **base, "message_id": "u-detected", "kind": "plate_detected",
                "upstream_pass_id": 30, "sequence": 10,
                "color": "yellow", "confidence": 0.95, "observations": 3,
            }))
            handoff._on_message(None, None, Message({
                **base, "message_id": "u-removed", "kind": "plate_removed",
                "upstream_pass_id": 30, "sequence": 10,
                "color": "yellow", "reason": "disappeared_before_exit",
            }))
            self.assertEqual(
                [row["handoff_status"] for row in handoff.rows],
                ["removed_at_upstream"],
            )

            upstream = {
                "session_id": "cam-3-removal", "upstream_pass_id": 31,
                "lap_id": 1, "sequence": 11, "color": "orange",
                "confidence": 0.96, "timestamp": time.time() - 3.8,
                "received_at": time.time() - 3.8, "final_received": True,
            }
            handoff._pending.append(upstream)
            state = {
                "pass_id": 12, "lap_id": 1, "first_seen_unix": time.time(),
                "tracker_ids": {41, 42},
                "local_color_votes": {"orange": 4},
            }
            self.assertEqual(handoff.match_downstream_entry(state), "orange")
            handoff.finalize_downstream_removed(state)
            statuses = [row["handoff_status"] for row in handoff.rows]
            self.assertEqual(statuses, [
                "removed_at_upstream", "removed_at_downstream"
            ])
            self.assertEqual(handoff.rows[1]["downstream_color"], "orange")

            handoff.save()
            summary = json.loads(
                (Path(temporary_directory) / "handoff_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["removed_at_upstream"], 1)
            self.assertEqual(summary["removed_at_downstream"], 1)


if __name__ == "__main__":
    unittest.main()
