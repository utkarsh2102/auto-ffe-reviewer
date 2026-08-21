"""Deterministic assessment: signals, scoring and hard gates.

Nothing here consults a model. The whole layer runs offline, is unit-testable,
and produces a usable record on its own -- which is what makes the system
worth something on a day the LLM is unavailable, and what stops the model
being asked to invent facts it has no way of knowing.
"""
