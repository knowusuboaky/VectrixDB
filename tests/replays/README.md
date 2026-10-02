# Replays: proof that a regression test catches the regression

A test that passes against the fix is half a test. The other half is showing
it fails against the bug. Each file here is a pytest plugin that restores one
old behaviour by monkeypatching; run the matching tests with it loaded and
they must go red:

```bash
python scripts/replay.py tests/replays/old_notin.py tests/unit/test_hierarchy_persistence.py::TestScale
```

The runner exits 0 only if at least one test fails under the replay, which is
the point, 1 if every test still passes, and 2 if pytest could not run them at
all: a wrong path, a plugin that does not import, or a selection that matches
nothing. Before the third case had its own exit, it read as a replay that
passed. Add a replay whenever you add a regression test for a bug worth
remembering; name it for the behaviour it restores.

| Replay | Restores | Caught by |
| --- | --- | --- |
| `old_behaviour.py` | `save_hierarchy` / `load_hierarchy` as no-ops | `test_hierarchy_persistence.py` |
| `old_gating.py` | searchers gated on a truthy hierarchy | `TestNoCommunitiesIsStillSearchable` |
| `old_notin.py` | `DELETE ... NOT IN (?, ?, ...)` bound per community | `TestScale` |
| `old_empty_return.py` | `_rebuild_searchers` early return on an empty graph | `TestEmptyGraphIsSearchable` |
| `old_tombstone_clamp.py` | asking the index for only the live document count | `TestSearchAfterDelete` |
| `old_acl_default_allow.py` | enterprise search allowing documents with no `_acl` | `TestEnterpriseSearch::test_enterprise` |
| `old_index_config.py` | a collection reporting default HNSW parameters | `TestCollectionsV2::test_hnsw_parameters_are_accepted` |
| `old_text_unit_content.py` | the REBEL and hybrid extractors reading `TextUnit.content` | `test_extractor_text_units.py` |
| `old_sync_full.py` | a sync that copied no rows and reported success | `test_sync_full.py` |
| `old_chunk_size_overrun.py` | chunks longer than the size asked for, and repeated offsets | `test_invariants_hold_for_any_text` |
| `old_empty_filter_allows_everything.py` | an explicit empty filter read as no filter | `test_search_helpers.py` |
| `old_dangling_relationships.py` | relationships left pointing at deduplicated-away entities | `test_graph_extractors.py` |
| `old_unbounded_wordpiece.py` | a WordPiece scan with no cap on word length | `test_long_token.py` |
| `old_forget_ignores_empty_ids.py` | the forget tool reading `ids=[]` as "no ids given" | `TestExplicitEmptyIsNotAbsent` |
| `old_acl_skipped_for_empty_principals.py` | ACL filtering skipped for a user in no groups | `TestExplicitEmptyIsNotAbsent` |
| `old_sync_selection_ignored.py` | `full(collections=[])` copying every collection | `TestExplicitEmptyIsNotAbsent` |
| `old_api_key_falls_back_to_the_environment.py` | `api_key=""` reaching for `OPENAI_API_KEY` | `TestExplicitEmptyIsNotAbsent` |
| `old_search_errors_became_empty_results.py` | a failed search answering `[]` or downgrading | `TestFailuresAreVisible` |
| `old_failed_collection_vanished.py` | a collection that would not load dropped in silence | `TestFailuresAreVisible` |
| `old_nan_thresholds_were_accepted.py` | a binary quantiser fitting to NaN and reporting success | `TestFailuresAreVisible` |
| `old_collection_name_was_a_path.py` | collection names used as paths unvalidated | `TestCollectionNamesAreValidated` |
| `old_rerank_had_no_vectors.py` | `exact` a no-op and `mmr` empty, for want of vectors | `TestArgumentsThatWereIgnored` |
| `old_sparse_model_silently_bm25.py` | an unreachable sparse model swapped for BM25 in silence | `TestArgumentsThatWereIgnored` |
| `old_opensearch_score_was_inverted.py` | an OpenSearch store handing the collection its raw score | `test_a_served_score_is_the_engines_own_and_a_threshold_keeps_the_best` |
| `old_heading_only_markdown_vanished.py` | a Markdown document of headings alone chunked to nothing | `test_invariants_hold_for_any_text` |
| `old_ollama_needed_requests.py` | the Ollama fallback importing the undeclared `requests` | `test_ollama_falls_back_to_plain_http_when_package_missing` |
| `old_missing_field_was_null.py` | a missing filter path indistinguishable from a null value | `test_types.py` |
| `old_membership_ignored_list_fields.py` | `$in`/`$nin` testing the whole field against the operand | `test_types.py` |
| `old_psutil_was_assumed_present.py` | the memory monitor dereferencing psutil unguarded | `test_clean_install.py` |
| `old_empty_principal_meant_no_restriction.py` | an empty principal value read as no restriction wanted | `test_policy.py` |
| `old_missing_entitlement_field_allowed.py` | a document with no entitlement metadata visible to all | `test_policy.py` |
| `old_one_suppressed_count.py` | one withheld count covering both in-scope and out-of-scope | `test_policy.py`, `test_audit.py` |
| `old_models_counted_the_english_one.py` | the bundled English model answering for the multilingual one in a download and a listing | `test_a_bundled_english_model_does_not_stand_in_for_a_download`, `TestModels` |
| `old_library_made_highlights.py` | highlights made for every keyword candidate of a library search, and dropped | `TestKeywordHalfSkipsHighlights` |
| `old_address_was_the_gateway.py` | the caller's address being the proxy's, behind a gateway | `TestWhoIsCalling` |
| `old_layers_read_the_raw_path.py` | roles, guests and masking reading the path with a gateway's prefix still on it | `TestBehindAGatewayPath` |
| `old_a_scoped_key_kept_the_rest.py` | the scope rule written the open way round, so a scoped key kept every route that names no collection | `test_signin_key_scope.py` |
| `old_ocr_read_across_columns.py` | OCR results sorted by the top of each box, so two columns interleaved and the words of a line came out of order | `TestReadingOrder`, `TestTextractIsPutInOrder` |
