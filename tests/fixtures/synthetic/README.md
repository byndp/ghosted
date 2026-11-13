# Synthetic transcript fixtures

Invented sessions in the real Claude Code JSONL shape: metadata lines first (`mode`, `queue-operation`,
`permission-mode`, `ai-title`, `atis-latch`, `file-history-snapshot`), conversation entries with the
usual envelope (`uuid`, `parentUuid`, `sessionId`, `timestamp` with ms and `Z`, `cwd`, `gitBranch`,
`version` 2.1.281, `entrypoint`, `userType`, `isSidechain`), one assistant entry per content block
(`thinking`, `text`, `tool_use`), tool results as `user` entries with `toolUseResult`, and the usual
trailing lines (`system` `turn_duration` / `stop_hook_summary`, `last-prompt`, `cost-state`,
`artifact-autoreact-ledger`). No real content: every name, path, prompt and reply is made up.

Expected results use `core.classify.classify(summary, DEFAULT_IGNORE_TAIL_PATTERNS)` (the config
defaults). Classifier v2 (contract 16.1): a question sentence that is a polite offer ("Should I open
tickets...?") is `offer_q` (tier soft); an offer about saving the work ("Want me to commit...?",
"Should I publish...?", or a phrase cue such as "want me to open the pull request") is `save_offer`
(tier waiting while the repo has uncommitted or unpushed work, else soft); a direct question stays
`trailing_q` (tier waiting). `tests/test_classify.py` checks this table against the files, and
`tests/test_transcript.py` checks that `parse_transcript` and `parse_lines` agree on every fixture.

Each file's entries share one UUID `sessionId`. To feed a fixture to the detector, copy it to
`<projects_dir>/<folder>/<sessionId>.jsonl` (the detector only reads UUID-named files).

| fixture | verdict | reason | what it covers |
|---|---|---|---|
| pos_limit_meta.jsonl | usage_limit | limit_meta | final API error entry: `error` rate_limit, status 429, `quotaLimits` |
| pos_limit_text_only.jsonl | usage_limit | limit_text | final text `Claude AI usage limit reached\|1759363200`, no error metadata |
| pos_limit_continue_limit.jsonl | usage_limit | limit_meta | limit, human `continue`, limit again |
| pos_limit_then_continue_unanswered.jsonl | usage_limit | limit_meta | limit (rate_limit, no quotaLimits), then a human `continue` that got no reply: still flagged |
| pos_trailing_question.jsonl | open_question | save_offer | ends "Want me to commit these 18 files?" (an offer to save the work) |
| pos_want_me_to.jsonl | open_question | save_offer | "want me to open the pull request" with no question mark: a phrase cue about saving the work |
| pos_say_the_word.jsonl | open_question | say_the_word | ends "say the word." |
| pos_your_call.jsonl | open_question | your_call | ends "Your call." |
| pos_question_then_nonhuman.jsonl | open_question | save_offer | "Should I publish the shorter one?", then only non-human entries: isMeta caveat, `/cost` command wrapper and stdout, bash-mode input/output, task-notification string with trailing text, task-notification and peer `queued_command` attachments, other attachments, compact boundary and summary, transcript-only entry, sidechain prompt, metadata `queue-operation`/`last-prompt` lines that hold human-looking text; first prompt carries a `system-reminder` that must be stripped |
| pos_question_url_and_code.jsonl | open_question | offer_q | real `?` at the end; the tail also has a URL with `?` and inline code with `?` |
| pos_pending_ask_user.jsonl | open_question | ask_user | a plain statement, then an `AskUserQuestion` tool use nobody answered (no tool_result, no turn end); only rule 4 can flag it |
| pos_pending_exit_plan.jsonl | open_question | plan_approval | plan mode: reads and a grep, then `ExitPlanMode` with the plan, never approved or rejected |
| neg_ask_user_answered.jsonl | clean | no_question | `AskUserQuestion`, then its tool_result with the answers, more work, and a clean final text |
| neg_safe_to_clear.jsonl | clean | no_question | ends "Safe to clear." |
| neg_qa_session.jsonl | clean | no_question | one human question, one statement answer, no tools |
| neg_answered_then_committed.jsonl | clean | no_question | question answered by the human, then "Committed." |
| neg_question_then_queued_human.jsonl | clean | human_after_final | question, then a human `queued_command` attachment (origin kind human) |
| neg_question_then_pasted_content.jsonl | clean | human_after_final | question, then a human `<pasted_content ...>` message (with a system-reminder) |
| neg_qmark_only_in_url.jsonl | clean | no_question | the only `?` is inside a URL query string |
| neg_qmark_only_in_code.jsonl | clean | no_question | the only `?` is inside inline code |
| neg_question_then_tool_use.jsonl | clean | tool_use_after_final | "Should I also update the docs? Doing that now." then tool_use + tool_result |
| neg_anything_else.jsonl | clean | ignored_tail | "Done. ... Anything else?" (default ignore pattern) |
| auto_sdk_cli_question.jsonl | open_question | offer_q | **automation fixture**: `entrypoint` sdk-cli on every entry. The classifier still says open_question; the detector must mark it `automated` |
| neg_api_server_error.jsonl | api_error | server_error | a question, a human "yes", then a final server_error API error (529): recorded, never alerted |
| neg_metadata_only.jsonl | empty | no_final_text | metadata lines only (every listed metadata type) |
| neg_sidechain_question.jsonl | clean | no_question | main chain ends clean; a later `isSidechain` assistant entry asks a question |
| neg_question_beyond_200.jsonl | clean | no_question | the only `?` is more than 200 characters before the end |
| neg_interrupted_by_user.jsonl | clean | human_after_final | question-looking text, then `[Request interrupted by user]` |
| neg_malformed_lines.jsonl | clean | no_question | truncated JSON, non-JSON, JSON array/string, blank, binary junk, and a partial last line with no newline, all holding `?` or "want me to"; otherwise ends clean |
| neg_limit_words_in_long_prose.jsonl | clean | no_question | a 300+ character reply that quotes the limit wording: not a usage limit (limit text is only trusted on a short final text) |

Not committed: `tests/test_transcript.py` builds a large transcript at runtime (a final question
followed by more than 600 KiB of non-human lines) so the tail window has to expand.

## Tier fixtures (`tiers/`)

Classifier v2 tier cases (contract 16.1), in a subfolder so the fixture-set tests (detector, e2e),
which treat every flagged top-level fixture not named `pos_*` as a false alarm, do not pick them up.
All are `open_question`; the reason decides the tier. Checked by `tests/test_classify.py`
(`TierFixtureTests`) against this table. The tier column is `classify.tier(verdict, reason)` with no
git facts (`unsaved=None`), as for a fixture: a `save_offer` row reads soft there, and the test also
checks it turns waiting with `unsaved=True`.

| fixture | verdict | reason | tier | what it covers |
|---|---|---|---|---|
| tiers/tier_direct_choice.jsonl | open_question | trailing_q | waiting | the spec mock: "Keep the sticky cart, or move it to the header?" |
| tiers/tier_should_i_choice.jsonl | open_question | trailing_q | waiting | "Should I keep A or switch to B?": starts like an offer but holds a real alternative |
| tiers/tier_offer_with_alternative.jsonl | open_question | save_offer | soft | "Want me to commit, or keep going...?": `want me to` wins over the alternative; commit makes it a save offer |
| tiers/tier_offer_non_save_alternative.jsonl | open_question | offer_q | soft | "Want me to update the README too, or keep going...?": the same shape with no save verb |
| tiers/tier_offer_then_statement.jsonl | open_question | save_offer | soft | "**Should I push it?** I'll leave it..." (emphasis, a statement after the question) |
| tiers/tier_direct_after_list.jsonl | open_question | trailing_q | waiting | a numbered list of options, then "Which one do you prefer...?" |
| tiers/tier_offer_code_or.jsonl | open_question | save_offer | soft | "- Ok to run `sync.sh --pick rebase or merge` here?" (list marker; an ` or ` inside code is not an alternative; a save verb inside code counts) |
| tiers/tier_offer_code_or_non_save.jsonl | open_question | offer_q | soft | "- Ok to run `sync.sh --pick ours or theirs` here?": the same with no save verb |
| tiers/tier_save_offer_phrase.jsonl | open_question | save_offer | soft | "Let me know if you want me to push the branch." (phrase cue, no `?`) |
| tiers/tier_save_noun_direct_question.jsonl | open_question | trailing_q | waiting | "Should the push notifications go out at 9am or at noon?": a save word in a direct question is not a save offer |
