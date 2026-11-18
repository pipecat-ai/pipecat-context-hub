# Vendored deprecation registry snapshot

Source: `pipecat-ai/pipecat`, tag `v1.16.0`, path `scripts/deprecations/`.
Files: `deprecations.json`, `removals.json` (verbatim, unmodified).

## Fetch commands

```bash
D=tests/fixtures/deprecations/v1.16.0
for f in deprecations.json removals.json; do
  gh api "repos/pipecat-ai/pipecat/contents/scripts/deprecations/$f?ref=v1.16.0" \
    -H "Accept: application/vnd.github.raw" > $D/$f
done
```

## Refresh rule

Pinned: never refresh implicitly. To move the pin, re-fetch both files at the new tag with the
commands above, recompute every count below with the builders, update this file and the tag
together, and review the test diff. Do not hand-edit the JSON.

## Counts

Machine-readable: one `key: integer` per line (snake_case), until the next heading. Counts come
from `build_deprecation_map_from_registry(deprecations.json)` followed by
`add_removals_from_registry(map, removals.json)`. A "member" is a record with `kind != "module"`
and a dot in `subject`; its "last segment" is the text after the final dot. Built-map member
subjects are map keys that equal a raw dotted member subject (the fully-qualified duplicate keys
are excluded). "Homes" of a name = number of distinct member subjects with that last segment.

```
raw_records_total: 558
raw_deprecations_records: 549
raw_removals_records: 9
raw_dotted_member_records: 445
raw_dotted_member_last_segments: 150
map_total_entries: 1076
map_member_subjects: 444
map_member_last_segments: 150
map_one_home_last_segments: 100
map_multi_home_last_segments: 50
map_model_homes: 59
```

Notes on measures:
- `raw_dotted_member_records` (445) exceeds `map_member_subjects` (444) because one subject
  appears twice in the raw records and collapses to one map key.
- `raw_records_total` = deprecations.json records + removals.json records.

## Removals-sourced dotted members with a unique last segment

`SarvamSTTService.set_prompt` exists (removals.json): map keys `SarvamSTTService.set_prompt` and
`pipecat.services.sarvam.stt.SarvamSTTService.set_prompt`; `set_prompt` has exactly 1 home.
Other removals members with 1 home: `AICFilter.create_vad_analyzer`,
`SpeechmaticsSTTService.UpdateParams`, `SpeechmaticsSTTService.update_params`.
Removals members with several homes: `language` (11), `voice_id` (35), `model` (59).
