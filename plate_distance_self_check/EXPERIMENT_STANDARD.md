# Plate Handoff Experiment and Decision Standard

Version: 1.0  
Status: Mandatory  
Scope: All detection, tracking, color, MQTT handoff, removal-location, alignment,
analysis, evaluation, and reporting work in this project.

## 1. Objective

The project exists to produce reproducible evidence for improving the automated
plate-detection and cross-camera handoff algorithms.  Reports are diagnostic
artifacts, not the objective.  No presentation layer may hide, repair, or
reinterpret an algorithm error.

The target is 100% exact event correctness on a defined, versioned validation
set under the documented installation conditions.  A claim of 100% must include
coverage, denominators, dataset version, code version, configuration, and all
error counts.

## 2. Non-negotiable principles

1. Raw observations are immutable.
2. Ground truth is supplied independently and is never inferred from algorithm
   output.
3. Predictions and ground truth are stored separately.
4. An algorithm decision must be traceable to its input observations, candidate
   alternatives, costs, thresholds, and code/configuration version.
5. Unknown or insufficient evidence is reported as `uncertain`; it must not be
   silently converted into a positive or negative result.
6. A manual interpretation may be recorded as an annotation, but must never
   overwrite raw data, algorithm output, or ground truth.
7. Markdown is a view of structured data.  JSON/CSV and Markdown must be
   generated from the same canonical result object.
8. Color from one camera must never overwrite another camera's local color.
9. Online alerts are provisional.  Final evaluation uses the finalized
   lifecycle and alignment result.
10. Every threshold or algorithm change requires replay against the same
    versioned validation cases and a before/after metric comparison.

## 3. Camera-neutral terminology

Do not encode permanent meanings such as `cam-3 = upstream` in the analysis
model.  Store the camera topology explicitly as directed edges:

```text
cam-A -> cam-B -> cam-C
```

Use these event locations:

- `removed_in_camera(camera_id)`
- `removed_between(source_camera_id, target_camera_id)`
- `camera_missed(camera_id)`
- `completed(camera_id)`

`upstream` and `downstream` may be used only as relative descriptions of one
topology edge.

## 4. Required data layers

Each layer is append-only or reproducibly derived from the preceding layer.

### 4.1 Raw observation layer

Canonical file: `raw_observations.jsonl`

Minimum fields:

```text
schema_version
run_id
camera_id
timestamp
frame_index
tracker_id
bbox_or_polygon
distance_cm
raw_color
stable_color
confidence
model_version
```

No alignment, removal classification, or manual correction belongs here.

### 4.2 Local lifecycle layer

Canonical file: `local_lifecycles.json`

Minimum fields:

```text
run_id
camera_id
local_pass_id
tracker_ids
first_seen_at
last_seen_at
entry_crossed_at
exit_crossed_at
distance_min_cm
distance_max_cm
distance_span_cm
observation_count
local_color
color_votes
confidence_statistics
status
evidence_refs
```

Allowed local statuses:

- `completed`
- `removed_local_suspected`
- `removed_local_confirmed`
- `reconnected`
- `uncertain`

### 4.3 Alignment candidate layer

Canonical file: `alignment_candidates.json`

For every allowed source/target candidate, store:

```text
source_camera_id
source_local_pass_id
target_camera_id
target_local_pass_id
travel_time_s
expected_travel_time_s
time_error_s
quality_penalty
total_cost
hard_gate_results
selected
rejection_reason
```

Candidates rejected by a hard gate must still be retained with the reason.

### 4.4 Algorithm decision layer

Canonical file: `algorithm_decisions.json`

Every decision must contain:

```text
decision_id
run_id
algorithm_name
algorithm_version
configuration_hash
decision_type
location
source_event_refs
target_event_refs
candidate_refs
reason_codes
confidence
provisional_or_final
created_at
```

Final decision types include:

- `matched`
- `removed_in_camera`
- `removed_between_cameras`
- `camera_missed`
- `downstream_extra`
- `tracker_fragmentation`
- `tracker_merge`
- `uncertain`

### 4.5 Ground-truth layer

Canonical file: `ground_truth.json`

Ground truth is entered independently after the run.  It must include the
original physical sequence and all intentional removals:

```json
{
  "schema_version": 1,
  "run_id": "example-run",
  "original_sequence": ["O", "O", "G"],
  "removals": [
    {
      "physical_sequence": 3,
      "color": "G",
      "location_type": "in_camera",
      "camera_id": "cam-2"
    }
  ],
  "annotations": []
}
```

Corrections create a new ground-truth revision; they do not mutate prior
revisions without an audit record.

### 4.6 Evaluation layer

Canonical files:

- `evaluation.json` -- authoritative metrics and error instances
- `evaluation_summary.md` -- generated only from `evaluation.json`

## 5. Color-source rules

- Removal inside a camera uses that camera's local color.
- Removal between two cameras uses the finalized source-camera color.
- A matched plate retains every camera's local color separately.
- Consensus color is a separate derived field and never replaces local values.
- Missing color evidence is `unknown`, not a value copied from another camera.

## 6. Online versus final decisions

Online notification states:

- `suspected`
- `confirmed`
- `revoked`

They share one immutable `event_id`.  A suspected notification may be emitted
quickly, but it is not counted as a final positive until confirmed.

Greedy FIFO may be retained for provisional display only.  Final metrics must
use the configured final alignment algorithm.  Provisional and final decisions
must be stored separately so that revision behavior can be evaluated.

## 7. Exact event matching for evaluation

A predicted removal is an **exact true positive** only when all required keys
match one ground-truth removal:

```text
physical_sequence
location_type
camera_id or source/target edge
```

Color correctness is evaluated separately and must not determine whether the
event is paired for evaluation.

Each prediction and truth event may participate in at most one evaluation
match.  Evaluation matching must be deterministic and versioned.

Definitions:

```text
TP = predicted removals matched exactly to a ground-truth removal
FP = predicted removals with no exact ground-truth match
FN = ground-truth removals with no exact prediction
```

## 8. Required quantitative metrics

Every evaluation must publish integer numerators and denominators in addition
to percentages.

### 8.1 Removal precision, recall, and F1

```text
precision = TP / (TP + FP)
recall    = TP / (TP + FN)
F1        = 2 * precision * recall / (precision + recall)
```

Zero-denominator results are `null`, not automatically 0% or 100%.

Metrics are required overall and separately for:

- removal inside each camera
- removal between each adjacent camera edge
- each color

### 8.2 Exact-event accuracy

```text
exact_event_rate = exact_TP / number_of_ground_truth_removals
```

This is reported alongside precision/recall; it does not replace them.

### 8.3 Sequence accuracy

For ground-truth removal events paired by location regardless of sequence:

```text
sequence_accuracy = correct_sequence / location_matched_events
```

Also report absolute sequence error:

```text
sequence_MAE = mean(abs(predicted_sequence - true_sequence))
```

### 8.4 Location accuracy

For events paired by physical sequence regardless of predicted location:

```text
location_accuracy = correct_location / sequence_matched_events
```

A location confusion matrix is mandatory.

### 8.5 Color accuracy

Color is evaluated only after event identity and location are matched:

```text
color_accuracy = correct_color / exact_identity_and_location_matches
```

A color confusion matrix is mandatory.  Unknown colors remain in the matrix.

### 8.6 Plate-count error

```text
count_error = predicted_removal_count - true_removal_count
absolute_count_error = abs(count_error)
```

Report overall and per location.

### 8.7 Coverage and uncertainty

```text
coverage = final_non_uncertain_decisions / eligible_events
uncertain_rate = uncertain_events / eligible_events
selective_accuracy = correct_final_decisions / final_non_uncertain_decisions
```

A 100% selective accuracy with low coverage is not a 100% system result.

### 8.8 Local detection and tracking metrics

When frame/plate ground truth is available, report per camera:

- plate detection precision/recall/F1
- completed lifecycle recall
- tracker fragmentation rate
- tracker merge rate
- mean tracker IDs per physical plate
- false local-pass count

Definitions:

```text
fragmentation_count(plate) = max(0, local_passes_for_plate - 1)
tracker_merge = one local pass assigned to more than one physical plate
```

### 8.9 Cross-camera alignment metrics

- alignment pair precision/recall/F1
- gap precision/recall/F1
- downstream-extra precision
- sequence drift count
- first drift position
- recovery distance in plates after a gap

### 8.10 Notification metrics

For provisional alerts:

```text
suspect_latency = suspected_at - physical_removal_at
confirm_latency = confirmed_at - physical_removal_at
revocation_latency = revoked_at - suspected_at
false_alert_rate = revoked_or_false_suspicions / all_suspicions
```

Report median, P90, P95, and maximum latency.

## 9. Mandatory error taxonomy

Every FP, FN, mismatch, and uncertain result must receive one or more reason
codes:

- `model_miss`
- `false_detection`
- `tracker_fragmentation`
- `tracker_merge`
- `color_misclassification`
- `local_lifecycle_incomplete`
- `alignment_wrong_pair`
- `alignment_wrong_gap`
- `travel_time_outlier`
- `mqtt_missing`
- `mqtt_duplicate`
- `camera_start_offset`
- `insufficient_evidence`
- `ground_truth_ambiguous`

An aggregate metric without its contributing error instances is incomplete.

## 10. Reproducibility metadata

Every run and replay must record:

```text
run_id
start/end timestamps
camera topology
camera device identities
model ID/version
source commit or source hash
configuration hash
ROI hash
algorithm name/version
thresholds
MQTT broker information
software environment
ground-truth revision
```

## 11. Change-control rule

Before changing an algorithm or threshold:

1. State the targeted error instances and hypothesis.
2. Preserve the current baseline evaluation.
3. Implement the smallest testable change.
4. Replay all versioned validation runs.
5. Publish before/after metrics and the exact changed error set.
6. Reject the change if it improves one case by regressing an accepted case,
   unless the tradeoff is explicitly approved and documented.

No decision may be justified only by a prettier aggregate percentage.

## 12. Acceptance criteria for a 100% claim

A result may be described as 100% only when all are true:

```text
FP = 0
FN = 0
exact sequence errors = 0
location errors = 0
color errors = 0
coverage = 100%
uncertain events = 0
all mandatory validation cases passed
```

The claim must name the validation-set version and operating conditions.  It
must not be generalized beyond that scope without additional validation.

