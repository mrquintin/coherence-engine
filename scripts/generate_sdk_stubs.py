#!/usr/bin/env python3
"""Deterministic SDK stub generator for the Coherence Fund API.

Prompt 17 of 20 — OpenAPI refresh + typed SDK stub generator.

Reads ``docs/specs/openapi_v1.yaml`` and emits a typed Python client at
``sdk/python/coherence_fund_client/client.py``. The generator is
reproducible: the same YAML produces byte-identical client source.

Usage::

    python scripts/generate_sdk_stubs.py            # write/overwrite
    python scripts/generate_sdk_stubs.py --check    # non-zero if drift

Design rules:

* Python stdlib only (plus the already-present PyYAML); the emitted
  client depends solely on stdlib ``urllib``.
* Operations are sorted by ``operationId`` before emission so the
  output order is stable regardless of YAML editor churn.
* The emitted header embeds the YAML's SHA-256 and the generator
  version so drift across branches is easy to spot.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


GENERATOR_VERSION = "1.0"
HTTP_METHODS: Tuple[str, ...] = ("get", "post", "put", "patch", "delete")

_DEFAULT_SPEC = Path("docs/specs/openapi_v1.yaml")
_DEFAULT_OUTPUT = Path("sdk/python/coherence_fund_client/client.py")
# Canonical label embedded in the generated file's header so the output
# stays byte-identical regardless of the absolute path the generator was
# invoked with (e.g. tmp dirs inside pytest).
_CANONICAL_SPEC_LABEL = "docs/specs/openapi_v1.yaml"


def _snake(name: str) -> str:
    """Convert an operationId / tag to snake_case deterministically."""
    # Replace non-alphanumerics with underscores.
    sub = re.sub(r"[^A-Za-z0-9]+", "_", name)
    # camelCase -> camel_Case
    sub = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", sub)
    # HTTPRequest -> HTTP_Request
    sub = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", sub)
    return sub.strip("_").lower()


def _resolve_ref(spec: Dict[str, Any], ref: str) -> Dict[str, Any]:
    if not ref.startswith("#/"):
        return {}
    parts = ref.lstrip("#/").split("/")
    node: Any = spec
    for p in parts:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _expand_parameters(
    spec: Dict[str, Any], params: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in params or []:
        if isinstance(p, dict) and "$ref" in p:
            out.append(_resolve_ref(spec, p["$ref"]))
        elif isinstance(p, dict):
            out.append(p)
    return out


def _extract_operations(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten ``paths`` into a sorted list of operation records."""
    ops: List[Dict[str, Any]] = []
    paths = spec.get("paths") or {}
    for path_template in sorted(paths.keys()):
        item = paths[path_template] or {}
        shared_params = _expand_parameters(spec, item.get("parameters") or [])
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not operation:
                continue
            op_id = str(operation.get("operationId") or "")
            if not op_id:
                continue
            tags = operation.get("tags") or []
            if tags:
                tag = _snake(tags[0])
            else:
                # Fall back to the first literal segment of the path so
                # method names stay informative when the YAML omits
                # explicit ``tags:`` entries (current state of
                # ``openapi_v1.yaml`` — see prompt 17 notes).
                segments = [
                    s
                    for s in path_template.strip("/").split("/")
                    if s and not s.startswith("{")
                ]
                tag = _snake(segments[0]) if segments else "default"
            local_params = _expand_parameters(spec, operation.get("parameters") or [])
            all_params = shared_params + local_params
            request_body = operation.get("requestBody") or {}
            body_required = bool(request_body.get("required", False))
            has_body = bool(request_body)
            ops.append(
                {
                    "path": path_template,
                    "method": method.upper(),
                    "operation_id": op_id,
                    "tag": tag,
                    "summary": str(operation.get("summary") or "").strip(),
                    "required_roles": list(operation.get("x-required-roles") or []),
                    "parameters": all_params,
                    "has_body": has_body,
                    "body_required": body_required,
                }
            )
    ops.sort(key=lambda o: (o["operation_id"], o["path"], o["method"]))
    return ops


def _path_param_names(path_template: str) -> List[str]:
    return re.findall(r"{([^{}]+)}", path_template)


def _kwarg_name(raw: str) -> str:
    snake = _snake(raw)
    # Python keywords we might bump into.
    if snake in {"class", "def", "import", "from", "return", "lambda", "pass"}:
        return snake + "_"
    return snake


_HEADER_TEMPLATE = '''"""Auto-generated Coherence Fund Python SDK client.

DO NOT EDIT BY HAND. Regenerate via::

    python scripts/generate_sdk_stubs.py

Source spec : {spec_path}
Spec SHA-256: {spec_sha256}
Generator   : {generator_version}
Operations  : {op_count}

The client depends only on the Python standard library so it can be
vendored into environments without ``requests``. Each endpoint method
is named ``<tag>_<operationId_snake>`` and returns the parsed JSON
response body. Non-2xx responses raise :class:`CoherenceFundClientError`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional


class CoherenceFundClientError(Exception):
    """Raised on non-2xx responses from the Coherence Fund API."""

    def __init__(self, status: int, body: Any, message: str = "") -> None:
        super().__init__(message or f"HTTP {{status}}")
        self.status = status
        self.body = body


class CoherenceFundClient:
    """Typed stdlib-only client for the Coherence Fund Orchestrator API.

    The ``base_url`` should include the API server prefix (e.g.
    ``https://fund.example.com/api/v1``). All methods return the
    decoded JSON envelope produced by the server.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
    ) -> Dict[str, Any]:
        url = f"{{self.base_url}}{{path}}"
        if query:
            cleaned = {{k: v for k, v in query.items() if v is not None}}
            if cleaned:
                url = f"{{url}}?{{urllib.parse.urlencode(cleaned, doseq=True)}}"
        merged: Dict[str, str] = {{"Accept": "application/json"}}
        if self.api_key:
            merged["X-API-Key"] = self.api_key
        if self.bearer_token:
            merged["Authorization"] = f"Bearer {{self.bearer_token}}"
        if headers:
            for k, v in headers.items():
                if v is None:
                    continue
                merged[str(k)] = str(v)
        data_bytes: Optional[bytes] = None
        if json_body is not None:
            merged.setdefault("Content-Type", "application/json")
            data_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, method=method, headers=merged)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {{}}
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return {{"raw": raw.decode("utf-8", errors="replace")}}
        except urllib.error.HTTPError as exc:
            body_bytes = b""
            try:
                body_bytes = exc.read()
            except Exception:
                body_bytes = b""
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
            except ValueError:
                body = {{"raw": body_bytes.decode("utf-8", errors="replace")}}
            raise CoherenceFundClientError(exc.code, body, f"HTTP {{exc.code}}") from exc
'''


def _render_method(op: Dict[str, Any]) -> str:
    path_template = str(op["path"])
    method = str(op["method"])
    op_id = str(op["operation_id"])
    tag = str(op["tag"])
    summary = str(op["summary"])
    required_roles = list(op["required_roles"] or [])

    method_name = f"{tag}_{_snake(op_id)}"
    path_params = _path_param_names(path_template)

    pos_args: List[str] = []
    kw_required: List[Tuple[str, str]] = []   # (alias_kwarg, original_name)
    kw_optional: List[Tuple[str, str]] = []   # (alias_kwarg, original_name)

    seen_kwargs = set()
    for pname in path_params:
        pos_args.append(f"{_kwarg_name(pname)}: str")

    for param in op["parameters"]:
        if not isinstance(param, dict):
            continue
        pin = str(param.get("in") or "")
        pname = str(param.get("name") or "")
        if not pname:
            continue
        if pin == "path":
            continue
        alias = _kwarg_name(pname)
        if alias in seen_kwargs or alias in {_kwarg_name(x) for x in path_params}:
            continue
        seen_kwargs.add(alias)
        required = bool(param.get("required", False))
        if required:
            kw_required.append((alias, pname))
        else:
            kw_optional.append((alias, pname))

    kw_required.sort(key=lambda t: t[0])
    kw_optional.sort(key=lambda t: t[0])

    if op["has_body"]:
        body_alias = "body"
        if op["body_required"]:
            kw_required.insert(0, (body_alias, "__body__"))
        else:
            kw_optional.insert(0, (body_alias, "__body__"))

    sig_parts: List[str] = ["self"] + pos_args
    if kw_required or kw_optional:
        sig_parts.append("*")
        for alias, original in kw_required:
            if original == "__body__":
                sig_parts.append(f"{alias}: Any")
            else:
                sig_parts.append(f"{alias}: Any")
        for alias, original in kw_optional:
            if original == "__body__":
                sig_parts.append(f"{alias}: Any = None")
            else:
                sig_parts.append(f"{alias}: Optional[Any] = None")
    signature = ", ".join(sig_parts)

    doc_lines: List[str] = []
    if summary:
        doc_lines.append(summary)
    doc_lines.append(f"HTTP {method} {path_template}")
    doc_lines.append(f"operationId: {op_id}")
    if required_roles:
        doc_lines.append("required roles: " + ", ".join(required_roles))

    body_lines: List[str] = []
    body_lines.append(f"    def {method_name}({signature}) -> Dict[str, Any]:")
    body_lines.append('        """' + doc_lines[0])
    for line in doc_lines[1:]:
        body_lines.append("")
        body_lines.append(f"        {line}")
    body_lines.append('        """')

    if path_params:
        fmt_args = ", ".join(f"{_kwarg_name(p)}={_kwarg_name(p)}" for p in path_params)
        body_lines.append(
            f'        path = "{path_template}".format({fmt_args})'
        )
    else:
        body_lines.append(f'        path = "{path_template}"')

    query_assignments: List[str] = []
    header_assignments: List[str] = []
    for alias, original in kw_required + kw_optional:
        if original == "__body__":
            continue
        source = None
        for param in op["parameters"]:
            if (
                isinstance(param, dict)
                and param.get("name") == original
                and param.get("in") in {"query", "header"}
            ):
                source = param
                break
        if source is None:
            continue
        pin = str(source.get("in"))
        if pin == "query":
            query_assignments.append(f'            "{original}": {alias},')
        elif pin == "header":
            header_assignments.append(f'            "{original}": {alias},')

    query_literal = "None"
    if query_assignments:
        body_lines.append("        query: Dict[str, Any] = {")
        for line in query_assignments:
            body_lines.append(line)
        body_lines.append("        }")
        query_literal = "query"

    header_literal = "None"
    if header_assignments:
        body_lines.append("        headers: Dict[str, Any] = {")
        for line in header_assignments:
            body_lines.append(line)
        body_lines.append("        }")
        header_literal = "headers"

    body_literal = "None"
    if op["has_body"]:
        body_literal = "body"

    body_lines.append(
        f'        return self._request("{method}", path, '
        f"query={query_literal}, headers={header_literal}, json_body={body_literal})"
    )
    return "\n".join(body_lines)


def render_client(spec: Dict[str, Any], yaml_sha256: str, spec_path: str) -> str:
    ops = _extract_operations(spec)
    header = _HEADER_TEMPLATE.format(
        spec_path=spec_path,
        spec_sha256=yaml_sha256,
        generator_version=GENERATOR_VERSION,
        op_count=len(ops),
    )
    parts: List[str] = [header]
    for op in ops:
        parts.append("")
        parts.append(_render_method(op))
    parts.append("")
    return "\n".join(parts)


def run(
    spec_path: Path,
    output_path: Path,
    *,
    check: bool,
) -> int:
    yaml_bytes = spec_path.read_bytes()
    yaml_sha256 = hashlib.sha256(yaml_bytes).hexdigest()
    spec = yaml.safe_load(yaml_bytes.decode("utf-8"))
    # Embed the canonical label (not the absolute path) so pytest's
    # tmp-path invocations and CLI invocations yield byte-identical output.
    rendered = render_client(spec, yaml_sha256, _CANONICAL_SPEC_LABEL)

    if check:
        if not output_path.exists():
            sys.stderr.write(
                f"[generate_sdk_stubs] MISSING {output_path}; "
                "run without --check to generate.\n"
            )
            return 1
        current = output_path.read_text(encoding="utf-8")
        if current != rendered:
            sys.stderr.write(
                f"[generate_sdk_stubs] DRIFT detected at {output_path}. "
                "Regenerate and commit.\n"
            )
            return 2
        sys.stdout.write(
            f"[generate_sdk_stubs] ok (sha256={yaml_sha256[:12]}..., "
            f"ops=?)\n"
        )
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(
        f"[generate_sdk_stubs] wrote {output_path} "
        f"(sha256={yaml_sha256[:12]}...)\n"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "SDK stub generator")
    parser.add_argument(
        "--spec",
        default=str(_DEFAULT_SPEC),
        help="Path to openapi_v1.yaml (default: docs/specs/openapi_v1.yaml)",
    )
    parser.add_argument(
        "--output",
        default=str(_DEFAULT_OUTPUT),
        help="Path to emit client.py (default: sdk/python/coherence_fund_client/client.py)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed client.py differs from regeneration output.",
    )
    args = parser.parse_args(argv)
    return run(Path(args.spec), Path(args.output), check=args.check)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
