"""Static checks run when a project is loaded.

These are what make secrecy a guarantee instead of a convention: a scenario that
could leak a secret field into a public step or platform fails to load.
"""

from __future__ import annotations

from jinja2 import Environment, TemplateSyntaxError, meta

from .schema import Project, Scenario

_jinja = Environment()


def template_variables(source: str) -> set[str]:
    return set(meta.find_undeclared_variables(_jinja.parse(source)))


def check_scenario(project: Project, sc: Scenario, step_types: set[str]) -> list[str]:
    p: list[str] = []
    where = f"scenario '{sc.id}'"
    secrets = sc.secret_fields()

    def known(name: str, ctx: str) -> bool:
        if name not in sc.fields:
            p.append(f"{where}: {ctx} refers to unknown field '{name}'")
            return False
        return True

    platform_texts_fields = [n for n, f in sc.fields.items() if f.type == "platform_texts"]
    if len(platform_texts_fields) > 1:
        p.append(f"{where}: only one field of type platform_texts is allowed")

    produced_by: dict[str, str] = {}
    seen_ids: set[str] = set()
    for step in sc.steps:
        sw = f"{where}, step '{step.id}'"
        if step.id in seen_ids:
            p.append(f"{sw}: duplicate step id")
        seen_ids.add(step.id)
        if step.use not in step_types:
            p.append(f"{sw}: unknown step type '{step.use}' (known: {', '.join(sorted(step_types))})")

        # inputs must exist and be produced earlier
        if step.sees.fields != "all":
            for name in step.sees.fields:
                if not known(name, f"step '{step.id}' sees"):
                    continue
                if name not in produced_by:
                    p.append(f"{sw}: sees field '{name}' that no earlier step produces")
                if name in secrets and not step.sees.secrets:
                    p.append(f"{sw}: sees secret field '{name}' but sees.secrets is false")

        if step.input is not None and known(step.input, f"step '{step.id}' input"):
            if step.input not in produced_by:
                p.append(f"{sw}: input '{step.input}' is not produced by an earlier step")
            if step.input in secrets:
                p.append(f"{sw}: input '{step.input}' is secret — media built from it would be public")
        if step.use == "tts" and step.input is None:
            p.append(f"{sw}: tts step needs 'input'")
        if step.use == "generate" and not step.produces:
            p.append(f"{sw}: generate step produces nothing")

        for name in step.produces:
            if not known(name, f"step '{step.id}' produces"):
                continue
            if name in produced_by:
                p.append(f"{sw}: field '{name}' is already produced by step '{produced_by[name]}'")
            produced_by[name] = step.id
            spec = sc.fields[name]
            # taint rule: anything derived from secrets is secret
            if step.sees.secrets and not spec.secret:
                p.append(
                    f"{sw}: sees secrets, so every produced field must be secret — '{name}' is not "
                    f"(mark it secret: true or move it to a step that does not see secrets)"
                )
            if spec.source == "caption" and not step.sees.caption:
                p.append(f"{sw}: field '{name}' has source: caption but the step does not see the caption")
            if spec.source.startswith("enrich.") and spec.source not in step_types:
                p.append(f"{sw}: field '{name}' needs external source '{spec.source}', which is not connected yet")
            if spec.type == "platform_texts" and step.use != "generate":
                p.append(f"{sw}: platform_texts can only be produced by a generate step")

    for name, spec in sc.fields.items():
        if name not in produced_by:
            p.append(f"{where}: field '{name}' is declared but no step produces it")

    # platforms
    for pname, target in project.enabled_platforms().items():
        pd = sc.platform_defaults.get(pname)
        pw = f"{where}, platform '{pname}'"
        if pd is None:
            p.append(f"{pw}: project publishes to {pname} but the scenario has no text rule for it")
            continue
        if pd.template is not None:
            try:
                variables = template_variables(pd.template)
            except TemplateSyntaxError as e:
                p.append(f"{pw}: template syntax error: {e}")
                continue
            for var in sorted(variables):
                if not known(var, f"platform '{pname}' template"):
                    continue
                if var in secrets and not pd.reveal_secrets:
                    p.append(f"{pw}: template uses secret field '{var}' but reveal_secrets is false")
        else:
            src = pd.text_from or ""
            base = src.split(".", 1)[0]
            if known(base, f"platform '{pname}' text_from"):
                if sc.fields[base].secret and not pd.reveal_secrets:
                    p.append(f"{pw}: text_from '{src}' is secret but reveal_secrets is false")
                if sc.fields[base].type == "platform_texts" and "." not in src:
                    p.append(f"{pw}: text_from must be 'platform_texts.<platform>'")

    # checks
    for chk in sc.checks:
        names = list(chk.secrets) + [n for n in (chk.options_field, chk.index_field, chk.field) if n]
        for name in names:
            known(name, f"check '{chk.type}'")
        for name in chk.secrets:
            if name in sc.fields and name not in secrets:
                p.append(f"{where}: check no_secret_leak lists '{name}', which is not secret")

    return p


def check_project(project: Project, step_types: set[str]) -> list[str]:
    problems: list[str] = []
    if project.scenario_selection.default not in project.scenarios:
        problems.append(f"scenario_selection.default '{project.scenario_selection.default}' is not a scenario")
    if not project.enabled_platforms():
        problems.append("no platforms enabled")
    for sc in project.scenarios.values():
        problems += check_scenario(project, sc, step_types)
    return problems
