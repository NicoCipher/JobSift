# Historical Operator State V1

## Provenance boundary

Target Universe V1 remains tied to its committed source-workbook SHA256, `58ab693d1928ad48443679653dc48d041746526c330015ff7af97f510b7b084d`. This validation does not edit that artifact.

Historical Operator State V1 records the operator-verified current export SHA256, `a7b84e2db189c97dde31ab154a08ade779d06caec868cefacb63e848823f64c3`, in its manifest. A fresh Google export retrieved for disposable local validation had SHA256 `5c75c8ff6f2fa98a3c9a80a8b78fd6b0f466ffd5f686fe371c6c6b8633f9def3`; spreadsheet ZIP bytes are not treated as a replacement for either recorded provenance value.

The fresh export rebuilt the committed Target Universe logical content exactly: all source-target records, bounded evidence, classifications, and aggregate counts matched. It therefore reconciles to the same logical 11,107-row corpus despite the byte-level SHA difference.

## Import validation

A disposable SQLite database imported all 11,107 historical link rows for client `taiwo`:

| Status | Rows |
| --- | ---: |
| Applied | 9,714 |
| Not Applied | 967 |
| Unknown / blank | 426 |

The import retained all 36 malformed/unusable URLs as provenance records. It derived 5,223 exact source identities only where a full supported posting identity was structurally present; 5,884 records use conservative normalized-URL suppression. There were 1,077 duplicated normalized URLs and 777 duplicated supported source identities in the operator evidence; no rows were dropped.

Re-importing the same client and workbook checksum inserted zero rows and reported all 11,107 as already present.

## Explicit blacklist evidence

The workbook contains `Blacklisted!!!!`, with 286 explicitly listed company entries and one instruction note. These records are stored separately from historical job links. They are deliberately not consumed by matching, delivery selection, or any inferred company policy in V1.

## Replay safety

Focused fixture coverage proves that a historical posting is still collected, persisted, and deterministically matched, but does not become a fresh CSV delivery for that client. An unseen posting still delivers. Status values do not change this behavior.
