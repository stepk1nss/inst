"""Extension point for external data sources (maps, web search, ...).

Not implemented in MVP. A field declared with `source: enrich.<provider>` makes
the project fail to load until a step named `enrich.<provider>` is registered
in contentbot.steps.STEP_TYPES. Such a step must:

  * produce only the fields it is responsible for,
  * return a warning for every value so the preview shows "check this",
  * never invent a value: empty string when the source has nothing.
"""
