"""Publishing modes: draft / confirm / auto."""

from __future__ import annotations

from .state import Validation

MODES = ("draft", "confirm", "auto")


def decide(mode: str, validation: Validation) -> str:
    """-> 'draft' | 'await_confirm' | 'publish'.

    Auto never overrides a check: any error, block_auto or needs_review flag
    downgrades it to manual confirmation (needs_review additionally locks the
    confirm button until a person reviews, see can_confirm).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode '{mode}'")
    if mode == "draft":
        return "draft"
    if mode == "auto" and not validation.errors and not validation.block_auto and not validation.needs_review:
        return "publish"
    return "await_confirm"


def review_pending(validation: Validation, reviewed: bool) -> bool:
    return bool(validation.needs_review) and not reviewed


def can_confirm(validation: Validation, reviewed: bool) -> bool:
    return not validation.errors and not review_pending(validation, reviewed)
