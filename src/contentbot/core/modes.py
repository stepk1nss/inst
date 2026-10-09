"""Publishing modes: draft / confirm / auto."""

from __future__ import annotations

from .state import Validation

MODES = ("draft", "confirm", "auto")


def decide(mode: str, validation: Validation) -> str:
    """-> 'draft' | 'await_confirm' | 'publish'.

    Auto never overrides a check: any error or block_auto flag downgrades it
    to manual confirmation.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode '{mode}'")
    if mode == "draft":
        return "draft"
    if mode == "auto" and not validation.errors and not validation.block_auto:
        return "publish"
    return "await_confirm"
