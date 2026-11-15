# Eval baseline: openai_compatible deepseek-flash

2026-11-15, one seed per case, in two runs (`20261115-113753`: the first seven cases,
stopped by the 3M-token limit; `20261115-115555`: the other five). 4.96M tokens in all.

Scored against the cases as committed. Three soft assertions were tightened after the first
run, because the model passed them too easily; its runs are scored here against the new
ones: `cube-hard-2x` `min_window_ms` 12-60 (was a floor of 12), `cube-medium-orb`
`min_window_ms` 25-100, and `fairer-tricky` `objects_deleted` at most 4.

| case | hard | soft failed | rounds | tokens | time |
|---|---|---|---|---|---|
| cube-easy-empty | pass | - | 6 | 95k | 36 s |
| cube-hard-2x | pass | min_window_ms 104.2 (want 12-60): not hard | 38 | 1.73M | 351 s |
| cube-medium-orb | pass | - | 22 | 798k | 238 s |
| explain-difficulty | pass | - | 2 | 23k | 5 s |
| explain-wall | pass | - | 8 | 112k | 27 s |
| extend-pattern | pass | - | 6 | 90k | 25 s |
| fairer-tricky | pass | objects_deleted 8 (want at most 4) | 30 | 953k | 233 s |
| fix-slot12 | pass | - | 12 | 169k | 29 s |
| fix-wall | pass | - | 9 | 126k | 27 s |
| recolour | pass | - | 4 | 48k | 9 s |
| speed-change | pass | - | 14 | 222k | 49 s |
| two-turns | pass | - | 28 | 593k | 198 s |

Hard assertions: 12 of 12 cases. Soft: 2 cases with a failure.

Notes:

- Every case that edited verified after its last edit (R4).
- Cost is in the long cases: a build that tries, checks, deletes and tries again re-sends
  the whole turn each round (about 45k tokens a round by the end). Three cases use 70% of
  the tokens.
- `speed-change` built 30 columns of 2x with 6 objects: thin, but no assertion covers it.
