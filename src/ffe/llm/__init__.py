"""The model-facing layer: policy bundling, harnesses, and the output contract.

Nothing in here knows anything about Feature Freeze. The review methodology
lives in policy/ as Markdown, and the harnesses take a system prompt, a user
message and a schema. That separation is what lets the model be swapped without
touching the review logic.
"""
