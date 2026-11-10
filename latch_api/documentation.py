"""Shared API documentation, without accessing runtime credentials or services."""
import json
from typing import Any, Literal

from fastapi import Response
from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str


class RequestErrorResponse(BaseModel):
    detail: str | list[dict[str, Any]]


class LivenessResponse(BaseModel):
    status: Literal['alive']


class ReadinessResponse(BaseModel):
    status: Literal['ready']
    profile_revision: str
    ssh_ca_fingerprint: str


class DeviceGrantResponse(BaseModel):
    grant_id: str
    device_id: str
    role: Literal['readonly', 'operator', 'admin']
    state: Literal['creating', 'active', 'revoking', 'cleanup_pending', 'revoked']
    expires_at: str = Field(description='UTC expiry timestamp in RFC 3339 format.')
    ssh_username: str = Field(description='Warpgate user:target selector; not an SSH password.')


def error_responses(**descriptions):
    """Explicit non-validation errors; FastAPI documents validation errors itself."""
    return {int(status): {'model': ErrorResponse, 'description': description}
            for status, description in descriptions.items()}


def yaml_text(value):
    """Serialize JSON-compatible OpenAPI values as YAML, quoting every scalar.

    JSON string quoting is valid YAML 1.2 and prevents descriptions, status-code
    keys and identifiers being interpreted as YAML booleans, dates or numbers.
    Block maps/lists keep the exported schema readable without a YAML dependency.
    """
    def lines(item, indent=0):
        prefix = ' ' * indent
        if isinstance(item, dict) and item:
            result = []
            for key, child in item.items():
                label = prefix + json.dumps(str(key), ensure_ascii=False) + ':'
                if isinstance(child, (dict, list)) and child:
                    result.extend([label, *lines(child, indent + 2)])
                else:
                    result.append(label + ' ' + json.dumps(child, ensure_ascii=False, allow_nan=False))
            return result
        if isinstance(item, list) and item:
            result = []
            for child in item:
                if isinstance(child, (dict, list)) and child:
                    result.extend([prefix + '-', *lines(child, indent + 2)])
                else:
                    result.append(prefix + '- ' + json.dumps(child, ensure_ascii=False, allow_nan=False))
            return result
        return [prefix + json.dumps(item, ensure_ascii=False, allow_nan=False)]

    return '\n'.join(lines(value)) + '\n'


def install_documentation(app):
    @app.get('/openapi.yaml', include_in_schema=False)
    def download_openapi():
        return Response(yaml_text(app.openapi()), media_type='application/yaml',
                        headers={'Content-Disposition': 'inline; filename="openapi.yaml"'})
