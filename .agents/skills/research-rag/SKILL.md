---
name: research-rag
description: Develop, diagnose and verify the Research Starship RAG workflow. Use for retrieval failures, comparison coverage, citation verification, RAG evaluation, recall@k, evidence gaps, or validating that answers are grounded in approved source versions.
---

# Research RAG

Use this skill for development and acceptance of this repository's RAG features. Do not replace the production retrieval path with an autonomous agent.

## Boundaries

- Production retrieval stays in `app/research/service.py`; structured planning stays in `app/research/query_planner.py`.
- Never treat planner output as evidence. Evidence must come from approved document versions in the current account and project.
- Keep L2 outbound approval and L3 network prohibition. Query planning and final generation have separate approval scopes.
- Do not bypass ambiguity by guessing. Multiple candidate documents require explicit user selection.
- Do not run local models, use SCIDOCS, reset production data, or claim mock results are real online results.

## Diagnose by component

1. Reproduce with public synthetic documents and a unique project.
2. Check whether the required passage was retrieved before changing prompts.
3. For comparisons, verify each entity has its own method chunk and, when requested, metric chunk. Baselines must not outrank the method described by the document.
4. If correct evidence is missing, inspect aliases, deduplication, reranking and comparison protection in `app/research/service.py`.
5. If correct evidence is present but the answer is wrong, inspect the generation prompt and citation checks.
6. Check approval scope before allowing any L2 request. If scope is incomplete, reject it rather than sending.

## Evaluation

Use small, versioned cases with question, expected document IDs, expected chunk IDs, acceptable answer facts, and required missing-entity warnings.

Measure separately:

- Retrieval recall@k and comparison-entity coverage.
- Citation correctness and document/version attribution.
- Structured analysis success rate.
- Unsupported-answer rate and correct no-evidence behavior.
- Latency, external-call count and estimated cost.

Run focused checks before full regression:

```bash
python -m pytest -q tests/research/test_query_planner.py tests/research/test_online_generation.py tests/research/test_citation_verification.py
node tests/unit/research_ia_ui.mjs
node tests/unit/research_state_ui.mjs
```

Do not report only pytest counts. Summarize actual retrieval and generation outcomes separately.

## Citation metadata verification

Use `POST /api/research/projects/{project_id}/citations/verify` for DOI, title, first-author surname and year checks. It uses Crossref only for bibliographic metadata. L1 may proceed directly, L2 requires approval of the exact citation metadata, and L3 is rejected.

A metadata match does not prove that a paper supports a sentence. The response deliberately returns `claim_verification=manual_required`; inspect the abstract or cited section before accepting the claim.

## Reference practices

- [RAG retrieval and generation diagnosis](https://github.com/nimadorostkar/Claude-Skills-collection/blob/main/skills/ai/rag/SKILL.md)
- [Citation metadata verification](https://github.com/KalarisLabs/research-agent-skills/blob/main/skills/citation-verification/SKILL.md)

These are design references, not runtime dependencies. Review third-party code before executing it.
