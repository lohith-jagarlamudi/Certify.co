# Verification rules

The verifier compares only the mapped fields shown in the Match Report. `issue_date` is excluded.

For each mapped field:

- Both values unavailable / insufficient -> ignore the field and continue.
- One side has a real value and the other side is unavailable -> `needs_review`.
- Both sides have real values and normalized values match -> matched.
- Both sides have real values and normalized values differ -> `not_verified`.
- At least one comparable field matched, with no mismatch or one-sided missing field -> `verified`.
- No comparable fields -> `ignore`.

A QR URL is used to select the verification page, but QR/certificate ID matching never overrides a real field mismatch or missing field.
Single-word recipient names (for example, `Daniels`) are valid and are not discarded by extraction.
