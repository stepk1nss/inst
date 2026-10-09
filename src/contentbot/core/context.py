"""What a step is allowed to see.

The core — not the step, not the prompt — decides a step's inputs. Secret
fields are removed unless the step declares sees.secrets; media and caption
are passed only when declared. A runtime assertion double-checks that no
secret value ends up in the input of a step that must not see secrets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.schema import Scenario, StepSpec
from .state import RunState


class SecretIsolationError(Exception):
    """A secret value reached a step that must not see secrets. Always a bug."""


@dataclass
class StepView:
    fields: dict[str, Any] = field(default_factory=dict)
    media: list[Path] = field(default_factory=list)
    caption: str | None = None


def visible_field_names(scenario: Scenario, step: StepSpec, available: dict[str, Any]) -> list[str]:
    secrets = scenario.secret_fields()
    wanted = list(available) if step.sees.fields == "all" else list(step.sees.fields)
    names = []
    for name in wanted:
        if name not in available:
            continue
        if name in secrets and not step.sees.secrets:
            continue  # the core drops it, whatever the config asked for
        names.append(name)
    return names


def build_view(scenario: Scenario, step: StepSpec, state: RunState, run_dir: Path) -> StepView:
    names = visible_field_names(scenario, step, state.fields)
    return StepView(
        fields={n: state.fields[n] for n in names},
        media=[run_dir / m.path for m in state.media] if step.sees.media else [],
        caption=state.caption if step.sees.caption else None,
    )


def public_fields(scenario: Scenario, values: dict[str, Any]) -> dict[str, Any]:
    secrets = scenario.secret_fields()
    return {k: v for k, v in values.items() if k not in secrets}


_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    return _NORM_RE.sub(" ", text.lower().replace("ё", "е")).strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def assert_no_secrets(scenario: Scenario, step: StepSpec, view: StepView) -> None:
    """Defence in depth: a step without sees.secrets must never receive a secret field.

    Checked on field identity, not on text: public fields may legitimately share
    words with a secret (a scene description mentions the same car the answer
    names). Whether published texts *reveal* a secret is the job of the
    no_secret_leak check.
    """
    if step.sees.secrets:
        return
    leaked = scenario.secret_fields() & set(view.fields)
    if leaked:
        raise SecretIsolationError(f"step '{step.id}' received secret fields: {', '.join(sorted(leaked))}")
