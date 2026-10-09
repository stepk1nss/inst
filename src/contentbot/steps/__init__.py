"""Step registry. A new step type = one module + one line here; projects then use it by name."""

from __future__ import annotations

from .base import Step
from .generate import GenerateStep
from .render_video import RenderVideoStep
from .tts import TTSStep

STEP_TYPES: dict[str, Step] = {
    s.name: s
    for s in (
        GenerateStep(),
        TTSStep(),
        RenderVideoStep(),
    )
}
