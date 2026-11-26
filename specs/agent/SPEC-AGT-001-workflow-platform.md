# SPEC-AGT-001: Workflow definitions (reusable platform capability)
Status: implemented     Owner: platform     Version: 1.0

## Context
Challenge 5 / Problem D: new use cases must reuse intelligence, integration, context and execution
rather than be rebuilt. Code: `ledgerflow/platform/workflow.py`, `workflows/*.yaml`.

## Requirements
- **REQ-AGT-001.1** [MUST] THE SYSTEM SHALL load each workflow from a YAML file declaring its persona prompt, allowed tools, actions, UI panels and voice, and every declared tool and action SHALL exist.
- **REQ-AGT-001.2** [MUST] A second workflow (credit cards) SHALL run on the same runtime, connectors, identity, actions, grounding, voice and UI, with new code limited to its YAML, prompt and domain tool module.
- **REQ-AGT-001.3** [SHOULD] WHEN the customer switches workflow THE SYSTEM SHALL keep their verified identity and carry the conversation so far as context.

## Acceptance criteria
- Given `workflows/`, both workflows load, and the card-only tools all live in `ledgerflow.domains.cards`. → .1, .2
- Given a switch from loans to cards, the session stays verified and the history starts with the earlier conversation. → .3
