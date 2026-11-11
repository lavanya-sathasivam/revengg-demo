# Knowledge Engine Architecture Specification

**Status:** Phase 1 design baseline  
**Branch:** `knowledge-engine/phase-01-contracts`  
**Scope:** Architecture and interface design only. No runtime implementation or database migration is included in this phase.

## 1. Purpose

Add an independent, locally executing AI Knowledge Engine to the existing Contract Intelligence & Legal Reasoning Engine. The Knowledge Engine converts contract documents and clauses into structured contractual facts and evidence-backed relationship assertions. The existing graph and reasoning components consume a validated projection of that knowledge.

The core contract-processing path must not call Google Gemini, OpenAI, or any other hosted AI inference API. Model artifacts may be obtained and installed separately, but inference runs locally.

This is a decision-support system. It does not independently determine enforceability, legal validity, or jurisdiction-specific legal outcomes.

## 2. Existing repository baseline to preserve

The current repository is an integrated FastAPI modular monolith with:
- a canonical application factory and separate demo/upload modes;
- firm-scoped upload authorization and contract/version provenance;
- PDF validation and text extraction in `extract.py`;
- clause segmentation, classification, references, and numeric helpers in `clauses.py`;
- deterministic graph construction and traversal in `graph.py`;
- atomic graph publication and database isolation through `adapter.py` and `db.py`;
- impact/counterfactual analysis in `impact.py`;
- keyword legal-authority retrieval in `retrieval.py`;
- an existing local deterministic legal-concept hashing embedding implementation in `embeddings.py`, with incremental clause indexing in `retrieval_index.py`.

The existing embedding implementation is not a neural transformer. It is a deterministic, 384-dimensional feature-hashing encoder with a legal concept lexicon; its interface can optionally use a Sentence-Transformers model. Phase 2 must benchmark and reuse/extend this existing capability rather than create a duplicate embedding subsystem.

The existing `nodes` and `edges` tables are a compact graph projection. They do not currently provide a complete normalized store for extracted facts, relation evidence, model provenance, or review state. The Knowledge Engine must not silently overload their current meanings.

Existing demo data, uploaded contract data, graph publication semantics, API behavior, firm scoping, version binding, and regression tests must remain compatible.

## 3. Architectural boundaries

The Knowledge Engine is a subsystem with these responsibilities:

1. **Ingestion and structure:** consume already validated document text and source locations; reuse the existing PDF and clause segmentation path where suitable.
2. **Contract classification:** classify a contract as one or more supported families, or mark it unknown/mixed.
3. **Legal information extraction:** extract parties/roles, obligations, rights, prohibitions, conditions, triggers, deadlines, durations, amounts, exceptions, and consequences into structured facts.
4. **Semantic relationship discovery:** generate candidate clause/fact pairs and classify typed relationships.
5. **Legal ontology and inference:** define controlled concepts, relation semantics, domain-specific constraints, and deterministic inference rules.
6. **Evidence and validation:** verify source spans, check graph/contract/version scope, preserve uncertainty, and route ambiguous assertions for review.
7. **Graph projection:** convert validated facts and relationships into the existing graph adapter's node/edge input contract.
8. **Evaluation and model lifecycle:** version local model artifacts and measure extraction/relationship quality on held-out contracts.

The Knowledge Engine does not own:
- authentication, firm membership, or authorization;
- original PDF storage;
- database connection management;
- the existing impact traversal algorithm;
- legal-authority retrieval;
- final legal advice.

## 4. Target runtime flow

```text
Authorized upload / existing contract version
  -> existing PDF validation and text extraction
  -> existing clause segmentation with source offsets
  -> Knowledge Engine contract classification
  -> domain pack selection
  -> clause classification and legal fact extraction
  -> normalized fact records
  -> semantic candidate generation
  -> trained relationship classification
  -> ontology/rule validation and evidence checks
  -> accepted / review-required / rejected assertions
  -> graph projection
  -> existing adapter's atomic graph publication
  -> existing impact and counterfactual reasoning
```

A failed or incomplete Knowledge Engine build must not publish a partial graph or make an incomplete graph appear analysis-ready. Existing last-good graph and failure-state behavior must be preserved.

## 5. Multiple contract families

The shared pipeline will support domain packs. Initial target families are:
- employment;
- commercial services;
- vendor/supplier;
- non-disclosure agreements;
- lease/rental;
- unknown and mixed documents.

A domain pack contains:
- family identifiers and detection cues;
- domain-specific clause labels and aliases;
- concepts and attributes;
- allowed/meaningful relationship patterns;
- deterministic validation rules;
- domain-specific evaluation examples.

Contract classification is multi-label-capable. The system must not force a single family when the document is mixed or evidence is insufficient. Unknown/mixed classification is a valid outcome.

The shared fact and relationship schemas remain stable across domains. Domain-specific details belong in typed attributes and versioned ontology packs, not incompatible per-domain database formats.

## 6. Model strategy

Use a hybrid local strategy:
- retain the current deterministic legal-concept hashing embedder as a baseline and fallback;
- benchmark a locally installed pretrained Sentence-Transformers model for semantic candidate generation;
- use local NLP components for linguistic processing and extraction features;
- train contract-specific extraction and relation-classification models using authorized, annotated examples;
- use deterministic ontology/rules for validation and well-defined inference.

Embeddings are for candidate generation and retrieval. Similarity is not proof of a contractual relationship and must never create an accepted legal edge by itself.

Model inference must be local. The core processing path must not fall back to Gemini/OpenAI when a local model fails. A model failure must produce a typed processing failure or an explicit reduced-capability result.

The existing Gemini-backed chat route is a separate application feature, not part of the proposed Knowledge Engine pipeline. The final local-only product decision must explicitly disable, remove, or replace that route before claiming the entire application has no external AI dependency.

## 7. Relationship semantics

Relationship labels are controlled by a versioned taxonomy. Every label has a written definition, source/target semantics, allowed endpoint types, evidence requirements, and evaluation examples.

Initial clause/fact relationship vocabulary:
- `contains`: contract -> clause;
- `creates`: clause -> legal fact/concept;
- `depends_on`: dependent provision -> dependency; preserve the existing dependent-first direction because impact traversal walks this edge backwards;
- `references`: referrer -> referenced provision; interpretation link, not automatically an impact dependency;
- `modifies`: modifying provision -> provision/fact modified;
- `triggered_by`: obligation/right -> triggering event;
- `subject_to`: governed obligation/right -> governing condition or limitation;
- `limits`: limiting provision -> provision/right limited;
- `exception_to`: exception provision -> general rule it qualifies;
- `survives_after`: continuing obligation/fact -> termination/expiry event;
- `conflicts_with`: canonical unordered clause pair with explicit conflict evidence; graph projection must use a deterministic representation.

New labels must not be added ad hoc by a model. Unknown relation labels are rejected. Existing `depends_on` and `references` behavior remains backward-compatible unless a separately reviewed migration changes it.

## 8. Evidence, confidence, and review

Every extracted fact and relationship assertion must retain:
- contract ID and version ID;
- source clause ID(s);
- source text offsets and page information where available;
- source text hash;
- extraction/classification method;
- model, model version, ontology version, and pipeline version;
- confidence score and the score's calibration/version metadata;
- validation status and review status.

Confidence is a model uncertainty signal, not a measure of legal correctness. Numeric acceptance thresholds must be set only after calibration on representative validation data; no arbitrary threshold is approved in Phase 1.

Evidence spans must resolve to the exact stored clause/version text. An inferred relationship without verifiable supporting spans must not be accepted as an evidence-backed edge.

Suggested assertion states:
- `proposed`;
- `accepted`;
- `needs_review`;
- `rejected`.

Suggested build states:
- `queued`;
- `processing`;
- `completed`;
- `completed_with_uncertainty`;
- `failed`;
- `unsupported`.

## 9. Persistence and graph integration

The existing `adapter.replace_graph()` contract accepts normalized nodes and indexed edges and publishes a complete graph atomically. It remains the publication boundary.

The current graph tables do not have sufficient fields to be the authoritative store for all Knowledge Engine facts and evidence. The preferred direction for a later schema design is:
- preserve existing `nodes`/`edges` as the graph projection consumed by current reasoning;
- persist normalized fact and relationship assertions in version/build-scoped sidecar records;
- link those records to contract, version, and graph build IDs;
- store a model/ontology/pipeline manifest for reproducibility;
- publish the projection only after all validation passes.

Exact table names, columns, migration strategy, and adapter changes are deferred to the relevant implementation phase after the current schema and compatibility tests are reviewed. Phase 1 makes no database changes.

All reads and writes must preserve current firm authorization and contract/version isolation. Cross-contract relationships are not created by ordinary per-contract ingestion.

## 10. Proposed service boundary

The Knowledge Engine should expose a framework-independent service interface conceptually equivalent to:

```python
analyze_contract(
    contract_id: int,
    version_id: int,
    clauses: list[ClauseInput],
) -> KnowledgeBuildResult
```

The result contains:
- contract classification;
- normalized legal facts;
- relationship assertions;
- evidence/provenance;
- uncertain/review-required items;
- model and ontology manifest;
- processing status and diagnostics.

The graph integration layer maps the accepted result to the existing adapter input format. The Knowledge Engine must not write directly to SQLite or bypass the adapter.

This is an interface design, not an implemented function.

## 11. Non-functional requirements

- **Local inference:** no external AI API calls from contract processing.
- **Determinism where possible:** identical input and pinned model/rule versions should be reproducible within documented numerical tolerances.
- **Fail closed:** unsupported formats, unknown labels, invalid spans, cross-contract endpoints, and incomplete builds cannot be published as valid knowledge.
- **Version isolation:** every fact and relation belongs to one contract version and build.
- **Auditability:** every accepted assertion has traceable source evidence and provenance.
- **Privacy:** customer uploads are not automatically training data.
- **Compatibility:** preserve current APIs, demo read-only mode, upload authorization, and impact behavior unless separately approved.
- **Extensibility:** new contract families use domain packs without changing the shared core schema.

## 12. Out of scope for Phase 1

- Installing or fine-tuning models;
- changing `requirements.txt`;
- implementing new extraction or relationship code;
- database migrations;
- changing `graph.py`, `adapter.py`, `impact.py`, or API routes;
- disabling the separate chat feature;
- claiming production readiness.

## 13. Phase 1 acceptance checklist

- [x] Existing repository architecture inspected.
- [x] Existing PDF, clause, graph, adapter, embedding, and retrieval components identified for reuse.
- [x] Shared architecture and module boundaries documented.
- [x] Multi-domain contract strategy documented.
- [x] Local hybrid model strategy documented.
- [x] Relationship direction and evidence requirements documented.
- [x] Graph projection and persistence boundary documented.
- [x] Existing Gemini chat path identified as separate from core contract processing.
- [ ] Data contracts reviewed and approved by project owner.
- [ ] Compatibility review completed against the current API and adapter tests.

Phase 1 is ready for review; it is not marked implementation-complete until the final two review items are approved.
