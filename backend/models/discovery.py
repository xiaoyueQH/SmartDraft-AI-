from backend.models.projects import DiscoveryNotes, DiscoveryUpdate


def merge_discovery(existing: dict, update: dict) -> dict:
    patch = DiscoveryUpdate.model_validate(update).model_dump(exclude_unset=True)
    result = dict(existing)
    for key in DiscoveryNotes.model_fields:
        if key not in patch:
            continue
        value = patch[key]
        result[key] = (
            list(dict.fromkeys([*result.get(key, []), *value]))
            if isinstance(value, list)
            else value
        )
    for key, removed in patch.get("remove_facts", {}).items():
        old = result.get(key, [])
        if isinstance(old, list):
            result[key] = [value for value in old if value not in removed]
        elif old in removed and key not in patch:
            result[key] = ""
    resolved = patch.get("resolved_questions", [])
    result["open_questions"] = [
        value for value in result.get("open_questions", []) if value not in resolved
    ]
    return DiscoveryNotes.model_validate(result).model_dump()
