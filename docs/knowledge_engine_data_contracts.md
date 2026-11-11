# Knowledge Engine Data Contracts

**Status:** Phase 1 proposal for review  
**Related specification:** [Knowledge Engine Architecture](knowledge_engine_architecture.md)  
**Important:** These are logical interface contracts. They are not database migrations or implemented Python models.

## 1. Contract input

The Knowledge Engine receives a contract version and clauses already extracted by the existing application.

```json
{
  "contract_id": 42,
  "version_id": 3,
  "clauses": [
    {
      "clause_id": 901,
      "section_number": "8.1",
      "parent_id": 880,
      "text": "The Employee shall keep Confidential Information confidential...",
      "page_number": 4,
      "char_start": 1820,
      "char_end": 1942,
      "clause_sha256": "<sha256>",
      "metadata": {
        "heading": "Confidentiality",
        "level": 1
      }
    }
  ]
}
```

Rules:
- `contract_id` and `version_id` must be validated by the application/adapter before processing.
- Every clause must belong to that contract and version.
- Source offsets refer to the exact stored text representation used by the existing clause row.
- The Knowledge Engine does not accept arbitrary client-supplied contract IDs as authorization.
- Raw clause text is input to local inference only; it is not written to audit metadata.

## 2. Contract classification

```json
{
  "labels": [
    {
      "family": "employment",
      "score": 0.93,
      "evidence": [
        {
          "clause_id": 901,
          "char_start": 0,
          "char_end": 48
        }
      ]
    }
  ],
  "status": "classified",
  "primary_family": "employment",
  "model_key": "contract-family-classifier",
  "model_version": "1.0.0",
  "ontology_version": "common-1.0.0"
}
```

Allowed initial family identifiers:
- `employment`;
- `commercial_services`;
- `vendor_supplier`;
- `non_disclosure`;
- `lease_rental`;
- `other`.

Classification status:
- `classified`;
- `mixed`;
- `unknown`;
- `needs_review`.

The numeric score above is illustrative only. It is not an approved threshold or measured model result. Multi-label classification is permitted. `primary_family` may be null for mixed/unknown results.

## 3. Legal fact

A legal fact is a normalized statement extracted from a clause. It is not a free-form summary and it is not a legal conclusion.

```json
{
  "fact_id": "v3-c901-f1",
  "contract_id": 42,
  "version_id": 3,
  "clause_id": 901,
  "fact_type": "obligation",
  "domain": "employment",
  "subject": {
    "entity_id": "party-employee",
    "label": "Employee",
    "role": "employee"
  },
  "predicate": "keep_confidential",
  "object": {
    "label": "Confidential Information",
    "concept": "confidential_information"
  },
  "modality": "mandatory",
  "polarity": "positive",
  "conditions": [],
  "trigger": null,
  "temporal": {
    "start_event": "employment_start",
    "end_event": null,
    "duration": null
  },
  "amount": null,
  "scope": null,
  "evidence": [
    {
      "clause_id": 901,
      "page_number": 4,
      "char_start": 0,
      "char_end": 48,
      "text_sha256": "<sha256>"
    }
  ],
  "extraction": {
    "method": "local_model",
    "model_key": "legal-fact-extractor",
    "model_version": "1.0.0"
  },
  "confidence": {
    "value": 0.91,
    "calibration_version": "pending"
  },
  "validation_status": "valid"
}
```

Initial `fact_type` vocabulary:
- `party_role`;
- `obligation`;
- `right`;
- `prohibition`;
- `condition`;
- `event`;
- `deadline`;
- `duration`;
- `payment`;
- `definition`;
- `exception`;
- `consequence`;
- `scope`.

Field semantics:
- `subject`: responsible or affected party/entity, when stated.
- `predicate`: normalized action or relation, not generated legal advice.
- `object`: target of the action, when stated.
- `modality`: `mandatory`, `permitted`, `prohibited`, `conditional`, or `unspecified`.
- `polarity`: `positive` or `negative`; negation must not be discarded.
- `conditions`: explicit condition facts or references.
- `trigger`: event that activates the fact, when identified.
- `temporal`: normalized time information with original evidence preserved.
- `amount`: exact numeric value and unit; do not silently convert unsupported units.
- `scope`: people, information, territory, services, or other scope limitations.

Optional values must be null or empty, not invented. Dates, durations, money, and percentages must retain units and source evidence. A fact may have multiple evidence spans.

## 4. Relationship assertion

A relationship assertion connects two clause or fact references. The direction is defined by the relationship type and must not be inferred from generic source/target naming.

```json
{
  "relationship_id": "v3-r1",
  "contract_id": 42,
  "version_id": 3,
  "source": {
    "ref_type": "clause",
    "ref_id": "901"
  },
  "target": {
    "ref_type": "clause",
    "ref_id": "914"
  },
  "relationship_type": "modifies",
  "direction_definition": "source modifies target",
  "evidence": [
    {
      "clause_id": 914,
      "page_number": 6,
      "char_start": 0,
      "char_end": 72,
      "text_sha256": "<sha256>"
    }
  ],
  "evidence_kind": "inferred",
  "discovery": {
    "candidate_method": "semantic_embedding",
    "classifier_key": "contract-relation-classifier",
    "classifier_version": "1.0.0",
    "rule_id": null
  },
  "confidence": {
    "value": 0.88,
    "calibration_version": "pending"
  },
  "validation_status": "passed",
  "review_status": "proposed"
}
```

Allowed initial relation types and direction:

| Type | Source | Target | Meaning |
|---|---|---|---|
| `contains` | Contract | Clause | Contract contains clause |
| `creates` | Clause | Fact/concept | Clause establishes fact |
| `depends_on` | Dependent clause/fact | Dependency clause/fact | Source relies on target; preserve current dependent-first convention |
| `references` | Referring clause | Referenced clause | Explicit cross-reference |
| `modifies` | Modifying clause | Modified clause/fact | Source changes or qualifies target |
| `triggered_by` | Obligation/right | Event | Event activates source |
| `subject_to` | Governed obligation/right | Condition/limitation | Source is governed by target |
| `limits` | Limiting provision | Limited provision/right | Source restricts target |
| `exception_to` | Exception provision | General rule | Source qualifies target |
| `survives_after` | Continuing obligation/fact | Termination/expiry event | Source continues after target event |
| `conflicts_with` | Canonically ordered clause | Other clause | Potential inconsistency; pair is semantically symmetric |

For `conflicts_with`, use a canonical pair identity to prevent duplicate assertions. Do not interpret a possible conflict as a final legal conclusion.

Relationship evidence may span one or both endpoints. A relation is not accepted solely because embedding similarity is high.

## 5. Evidence span

```json
{
  "clause_id": 914,
  "page_number": 6,
  "char_start": 0,
  "char_end": 72,
  "text_sha256": "<sha256>"
}
```

Offsets are zero-based, end-exclusive, and relative to the exact clause text version. The application must verify that the span is within bounds and that the clause belongs to the same contract/version as the assertion. Evidence quotes should be reconstructed from the stored clause text rather than trusted from model-generated text.

## 6. Confidence and review states

Confidence values are model outputs and must be calibrated before they are treated as probabilities. Phase 1 does not set a universal numeric threshold.

Validation status:
- `pending`;
- `passed`;
- `failed`;
- `not_applicable`.

Review status:
- `proposed`;
- `accepted`;
- `needs_review`;
- `rejected`.

Only assertions that pass schema, evidence, contract/version scope, endpoint, and ontology validation may be considered for graph publication. Whether a passed model assertion is automatically accepted or requires human review is a policy configured after calibration and evaluation.

## 7. Knowledge build result

```json
{
  "contract_id": 42,
  "version_id": 3,
  "status": "completed_with_uncertainty",
  "contract_classification": {},
  "facts": [],
  "relationships": [],
  "uncertain_items": [],
  "rejected_candidates_count": 0,
  "manifest": {
    "pipeline_version": "0.1.0",
    "fact_schema_version": "1.0.0",
    "relationship_schema_version": "1.0.0",
    "ontology_version": "common-1.0.0",
    "models": []
  },
  "diagnostics": []
}
```

Build statuses:
- `queued`;
- `processing`;
- `completed`;
- `completed_with_uncertainty`;
- `failed`;
- `unsupported`.

The result must distinguish zero discovered relationships from a failed relationship stage. Missing model artifacts must not silently downgrade the run to a misleading successful result.

## 8. Existing graph adapter projection

The Knowledge Engine result is not passed directly into the database. A dedicated integration mapper produces the existing adapter input:

```python
nodes = [
    {
        "ref": "8.1",
        "node_type": "clause",
        "detail": "Original clause reference"
    },
    {
        "ref": "8.1::obligation::1",
        "node_type": "obligation",
        "detail": "Normalized obligation description"
    }
]

edges = [
    {
        "source": 0,
        "target": 1,
        "relationship_type": "creates"
    }
]
```

These shapes reflect the existing `adapter.replace_graph()` contract: node refs/types/details and edge indexes into the node list. The mapper must preserve current edge-direction semantics and only project relationship types understood by the existing reasoning engine. New relation types must not accidentally enter impact traversal until their semantics and handling are implemented and tested.

The existing adapter remains the only graph publication boundary. The Knowledge Engine must not write graph rows directly.

## 9. Persistence direction for a later phase

The current `nodes`/`edges` schema is insufficient as the sole durable representation of facts, evidence spans, confidence, classifier/rule provenance, and review state.

A later migration design should consider version/build-scoped sidecar persistence for:
- normalized facts;
- relationship assertions;
- evidence spans;
- model/ontology/pipeline manifest;
- review and validation status.

The existing graph remains the compatibility projection for impact traversal. Exact SQL tables, indexes, constraints, migration steps, and rollback behavior are not approved in Phase 1.

## 10. Compatibility and validation invariants

1. A fact or relationship cannot reference a clause from another contract.
2. A fact or relationship cannot mix contract versions.
3. All source and target references must resolve within the same build scope.
4. Evidence offsets must resolve against the exact clause text and hash.
5. Unknown fact or relation types are rejected, not silently accepted.
6. A high similarity score cannot independently create a relationship.
7. The existing `depends_on` edge remains dependent-first.
8. `references` remains an interpretation link, not automatically an impact edge.
9. A failed build cannot publish a partial graph or replace a last-good graph.
10. Reprocessing the same contract version with the same pinned pipeline/model versions must be idempotent or produce a documented new build.
11. Customer uploads are not automatically training data.
12. Existing demo mode remains read-only and existing upload authorization remains authoritative.

## 11. Phase 1 review checklist

- [x] Contract input contract mapped to existing contract/version/clause records.
- [x] Fact schema proposed.
- [x] Relationship assertion schema proposed.
- [x] Evidence span semantics defined.
- [x] Confidence and review states defined.
- [x] Knowledge build result defined.
- [x] Existing graph adapter projection documented.
- [x] Existing embedding and retrieval capability accounted for.
- [ ] Project owner approval of fact taxonomy and relationship directions.
- [ ] Project owner approval of persistence direction before Phase 8 schema work.
