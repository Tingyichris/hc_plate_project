# Project instructions

All work in this directory must follow `EXPERIMENT_STANDARD.md`.

Before changing detection, tracking, MQTT handoff, alignment, removal
classification, evaluation, or reporting behavior:

1. Read `EXPERIMENT_STANDARD.md` completely.
2. Preserve raw evidence and keep predictions separate from ground truth.
3. Make every algorithm decision traceable to evidence, candidates, costs,
   thresholds, and code/configuration version.
4. Generate Markdown only from the same canonical structured result used for
   JSON/CSV output.
5. Add or update replayable tests and report before/after quantitative metrics.
6. Never label an inferred or manually interpreted result as ground truth.
7. Never claim 100% unless the acceptance criteria in the standard are met.

When requirements conflict, data integrity, traceability, reproducibility, and
honest uncertainty take priority over presentation convenience.
