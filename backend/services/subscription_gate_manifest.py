import json
from pathlib import Path


_SUBSCRIPTION_GATE_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "manifests"
    / "subscription"
    / "subscriptionGateManifest.json"
)


def _runninghub_model_id(workflow_id):
    value = str(workflow_id or "").strip()
    return f"runninghub/{value}" if value else ""


def _string_tuple(value, *, lowercase=False):
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for item in value:
        text = str(item or "").strip()
        if lowercase:
            text = text.lower()
        if text:
            out.append(text)
    return tuple(out)


def _legacy_alias_tuple(value, index):
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for alias_index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(
                f"subscription gate entry #{index} legacyAlias #{alias_index} must be an object"
            )
        alias_value = str(item.get("value") or "").strip()
        delete_when = str(item.get("deleteWhen") or "").strip()
        if not alias_value or not delete_when:
            raise ValueError(
                f"subscription gate entry #{index} legacyAlias #{alias_index} missing value or deleteWhen"
            )
        out.append({"value": alias_value, "deleteWhen": delete_when})
    return tuple(out)


def _load_subscription_gate_document():
    with _SUBSCRIPTION_GATE_MANIFEST_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("subscription gate manifest must be a JSON object")
    if str(data.get("schemaVersion") or "").strip() != "1.0":
        raise ValueError("unsupported subscription gate manifest schemaVersion")
    return data


def _normalize_gate_entry(item, index):
    if not isinstance(item, dict):
        raise ValueError(f"subscription gate entry #{index} must be an object")
    model_id = str(item.get("modelId") or "").strip()
    if not model_id:
        raise ValueError(f"subscription gate entry #{index} missing modelId")
    return {
        "key": str(item.get("key") or "").strip(),
        "modelId": model_id,
        "workflowId": str(item.get("workflowId") or "").strip(),
        "displayName": str(item.get("displayName") or "").strip() or model_id,
        "aliases": _string_tuple(item.get("aliases")),
        "legacyAliases": _legacy_alias_tuple(item.get("legacyAliases"), index),
        "providers": _string_tuple(item.get("providers"), lowercase=True),
        "modelPrefixes": _string_tuple(item.get("modelPrefixes")),
    }


def _load_subscription_gate_manifests():
    data = _load_subscription_gate_document()
    raw_gates = data.get("gates")
    if not isinstance(raw_gates, list):
        raise ValueError("subscription gate manifest gates must be an array")
    manifests = tuple(
        _normalize_gate_entry(item, index)
        for index, item in enumerate(raw_gates, start=1)
    )
    if not manifests:
        raise ValueError("subscription gate manifest gates must not be empty")
    return manifests


def _load_canonical_excludes():
    data = _load_subscription_gate_document()
    return frozenset(_string_tuple(data.get("canonicalExcludes")))


SUBSCRIPTION_GATE_MANIFESTS = _load_subscription_gate_manifests()
SUBSCRIPTION_GATE_CANONICAL_EXCLUDES = _load_canonical_excludes()


def get_subscription_gate_manifest_path():
    return str(_SUBSCRIPTION_GATE_MANIFEST_PATH)


def iter_subscription_gate_manifests():
    return tuple(dict(item) for item in SUBSCRIPTION_GATE_MANIFESTS)


def get_subscription_gate_model_ids():
    return tuple(
        str(item.get("modelId") or "").strip()
        for item in SUBSCRIPTION_GATE_MANIFESTS
        if str(item.get("modelId") or "").strip()
    )


def get_subscription_gate_model_id_by_key(key):
    target_key = str(key or "").strip()
    if not target_key:
        return ""
    for item in SUBSCRIPTION_GATE_MANIFESTS:
        if str(item.get("key") or "").strip() == target_key:
            return str(item.get("modelId") or "").strip()
    raise KeyError(f"Missing subscription gate manifest entry: {target_key}")


def get_subscription_gate_model_name_map():
    return {
        str(item.get("modelId") or "").strip(): str(item.get("displayName") or "").strip()
        for item in SUBSCRIPTION_GATE_MANIFESTS
        if str(item.get("modelId") or "").strip()
    }


def get_runninghub_subscription_workflow_ids():
    return {
        str(item.get("workflowId") or "").strip()
        for item in SUBSCRIPTION_GATE_MANIFESTS
        if str(item.get("modelId") or "").strip().startswith("runninghub/")
        and str(item.get("workflowId") or "").strip()
    }


def _build_alias_map():
    aliases = {}
    for item in SUBSCRIPTION_GATE_MANIFESTS:
        model_id = str(item.get("modelId") or "").strip()
        if not model_id:
            continue
        aliases[model_id] = model_id
        workflow_id = str(item.get("workflowId") or "").strip()
        if workflow_id:
            aliases[_runninghub_model_id(workflow_id)] = model_id
        for alias in item.get("aliases") or ():
            alias_text = str(alias or "").strip()
            if alias_text:
                aliases[alias_text] = model_id
        for alias in item.get("legacyAliases") or ():
            alias_text = str(alias.get("value") or "").strip()
            if alias_text:
                aliases[alias_text] = model_id
    return aliases


def _build_prefix_rules():
    rules = []
    for item in SUBSCRIPTION_GATE_MANIFESTS:
        model_id = str(item.get("modelId") or "").strip()
        if not model_id:
            continue
        for prefix in item.get("modelPrefixes") or ():
            prefix_text = str(prefix or "").strip()
            if prefix_text:
                rules.append((prefix_text, model_id))
    return tuple(rules)


_SUBSCRIPTION_GATE_ALIAS_MAP = _build_alias_map()
_SUBSCRIPTION_GATE_PREFIX_RULES = _build_prefix_rules()


def normalize_subscription_gate_model_id(value):
    model_id = str(value or "").strip()
    if not model_id:
        return ""
    if model_id in SUBSCRIPTION_GATE_CANONICAL_EXCLUDES:
        return model_id
    mapped = _SUBSCRIPTION_GATE_ALIAS_MAP.get(model_id)
    if mapped:
        return mapped
    for prefix, target_model_id in _SUBSCRIPTION_GATE_PREFIX_RULES:
        if model_id.startswith(prefix):
            return target_model_id
    return model_id
