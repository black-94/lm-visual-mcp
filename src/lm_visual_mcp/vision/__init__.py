"""Vision module: image-recognition capability.

The provider-neutral types, concrete providers, registry and router now live in
the sibling ``lm_visual_mcp.providers`` package; this package keeps only the
piece that gives them purpose:

- ``prompts``: task-aware system prompts for the vision tools.
- ``service``: the single entry point owning the provider router and the
  concurrency gate.
"""