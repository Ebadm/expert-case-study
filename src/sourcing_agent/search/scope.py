"""Which organisations a brief and a group are about (shared by the query builders)."""

from __future__ import annotations

from ..models import (
    BriefSpec,
    ExpertGroup,
    Organisation,
)


def organisations_in_spec(spec: BriefSpec, extra: list[Organisation] = ()) -> list[Organisation]:
    """Every organisation the brief mentions (target, named eligible, excluded), de-duplicated by name."""
    found: dict[str, Organisation] = {}
    candidates = [spec.target_organisation] if spec.target_organisation else []
    candidates += [o for g in spec.groups for o in g.eligible_organisations]
    candidates += [ex.organisation for ex in spec.excluded_organisations]
    candidates += list(extra)
    for o in candidates:
        found.setdefault(o.name.lower(), o)
    return list(found.values())


def excluded_organisations_for(spec: BriefSpec, group: ExpertGroup) -> list[Organisation]:
    return [
        ex.organisation for ex in spec.excluded_organisations
        if not ex.applies_to_group_ids or group.group_id in ex.applies_to_group_ids
    ]


def target_if_group_is_relative_to_it(spec: BriefSpec, group: ExpertGroup) -> Organisation | None:
    """The target, when the group is described relative to it (e.g. "customers of <target>")."""
    target = spec.target_organisation
    if target is None:
        return None
    types_text = " ".join(group.eligible_organisation_types).lower()
    if any(n.lower() in types_text for n in [target.name, *target.aliases]):
        return target
    return None
