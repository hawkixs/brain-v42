# Independent second opinion on the feature-embedding question (Codex CLI, gpt-6-astra)

**Reviewed:** `docs/superpowers/specs/2026-09-21-feature-embedding-definition.md` at `825e46da`

**Reviewer:** OpenAI Codex CLI 0.153.4, model `gpt-6-astra`, reasoning effort `high`, sandbox
`read-only`, non-interactive (`codex exec`), requested on 2026-09-21 for a frontier opinion from
a different family than the `gpt-5.6-terra` review of the reindex path earlier the same day. It
was asked for judgement on four questions, not a line-by-line review, and told to commit to an
answer.

**Outcome: it refuted two load-bearing claims of the document, and both refutations were
verified against the code and re-measured before this file was written.** See the corrections
recorded in the document itself.

---

**1. The dilemma is mostly manufactured.** Recovering an undocumented historical embedding input is a real problem; reproducibility versus useful matching is not an inherent trade-off. Different texts embedded by the same model do not thereby occupy incompatible spaces. Nor is `description` the only reproducible candidate: [explicit creation already uses persisted `name + description`](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/services/feature_creation_service.py:87), and merging embeds exactly the description it saves. The plan path does have a concrete mismatch: it embeds title plus summary/preamble but passes only the title as feature text. That is an inconsistent input contract, not an impossibility theorem. Preserving the first signal’s vector also does not establish that it represents the feature well.

**2. The 34% is a warning, not the claimed forecast.** Taking the reported measurement as given—I did not access production—it says those historical pairs miss the direct-link threshold under the proposed representation. It does **not** say they create features: [the resolver considers five candidates, reranks below 0.70, can merge, and falls back to linking at 0.65](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/services/cluster_guard.py:138). Another candidate might also win. Holding the provider constant is sensible, but without the current-vector baseline this does not even establish how many decisions change. I would compare complete resolver outcomes before/after, including candidate eligibility, top-five recall, wrong links, merges and creations, against independently judged matches and hard negatives. Repeat for the target provider. Retuning on historical positives alone invites false links.

**3. B is underrated; D is oversold; a fifth option is missing.** B does not cost 81 existing links: the upsert preserves plan identity and [link insertion deletes nothing](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/services/plan_indexer.py:639). It does require handling `skipped` instead of asserting a feature exists. D provides provenance, but cannot recover lost historical inputs or guarantee matching quality; it also needs a versioned model/composition contract. A is a legitimate simplification, not inherently inferior to D. C is sequencing, not an alternative architecture. **E is to separate embedding refresh from feature resolution:** re-embedding unchanged plans need not reopen their feature assignments. The document treats today’s unconditional call to `resolve()` as a requirement. It is an implementation choice.

**4. I would take E, coupled with writer alignment.** Make a provider-only rebuild preserve existing relationships and perform no feature creation or merging; reserve resolution for new or meaningfully changed signals, with explicit reassignment when needed. Standardize feature embeddings on a versioned, row-derived recipe—`name + description` is a reasonable starting candidate—and validate the complete resolver before cutover. This addresses the avoidable migration hazard and the inconsistent writers without pretending that storing source text proves semantic quality. Add D if retaining exact historical inputs has independent value. The drift checker should verify the chosen contract; it should not dictate what a feature means.