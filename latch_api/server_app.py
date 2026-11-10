"""Authenticated server orchestration, separate from public Nova vendor data."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from latch_api.device_access import AccessError
from latch_api.documentation import (LivenessResponse, RequestErrorResponse,
                                     error_responses, install_documentation)
from latch_api.server_access import Server, ServerAccess, ServerGateway

logger = logging.getLogger('uvicorn.error')


class ServerGrantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    server_id: UUID = Field(description='An instance in the operator-controlled server inventory.')
    public_key: str = Field(min_length=20, max_length=16384,
                            description='Laptop OpenSSH public key; authenticates only to Warpgate.')
    ttl_seconds: int = Field(default=300, ge=1, le=300, strict=True)


class ServerGrantResponse(BaseModel):
    grant_id: str
    server_id: str
    state: Literal['creating', 'active', 'revoking', 'cleanup_pending', 'revoked']
    expires_at: str = Field(description='UTC expiry in RFC 3339 format.')
    gateway_host: str
    gateway_port: int
    gateway_host_public_key: str = Field(description='Trusted gateway SSH host key, not the server key.')
    ssh_username: str = Field(description='Warpgate user:target SSH selector.')


def create_app(access, token, principal='lab-operator', sweep_interval=2):
    if len(token) < 32 or not principal:
        raise ValueError('Configure an owner-only API token of at least 32 characters')

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(access.sweep)

        async def expire():
            while True:
                await asyncio.sleep(sweep_interval)
                try:
                    await asyncio.to_thread(access.sweep)
                except Exception:
                    logger.error('Server grant reconciliation failed; retry scheduled')

        task = asyncio.create_task(expire())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title='Latch server access POC', version='1.0.0', docs_url=None,
                  redoc_url='/docs', lifespan=lifespan,
                  description='Short-lived, recorded SSH through Warpgate with OpenBao certificate injection.')
    install_documentation(app)
    bearer = HTTPBearer(auto_error=False, scheme_name='LatchBearer',
                        description='Owner-only POC API token; caller identity is configured server-side.')

    def authenticate(request: Request, credential=Depends(bearer)):
        authorization = request.headers.get('Authorization')
        if authorization is None or not hmac.compare_digest(authorization.encode(), ('Bearer ' + token).encode()):
            raise HTTPException(401, 'Authentication required', headers={'WWW-Authenticate': 'Bearer'})
        return principal

    def invoke(operation, *args):
        try:
            return operation(*args)
        except AccessError as error:
            raise HTTPException(error.status_code, str(error)) from None

    errors = error_responses(**{'401': 'Missing or invalid bearer token.',
                               '403': 'Caller is not authorized for this server.',
                               '404': 'Grant not found or belongs to another caller.',
                               '409': 'Idempotency key conflicts with an earlier request.',
                               '503': 'Provisioning failed; cleanup has been scheduled.'})

    @app.get('/health/live', response_model=LivenessResponse, tags=['Health'], operation_id='serverLiveness')
    def live():
        return {'status': 'alive'}

    @app.post('/v1/server-grants', status_code=201, response_model=ServerGrantResponse,
              responses={**errors, 422: {'model': RequestErrorResponse,
                         'description': 'Invalid request, TTL or SSH public key.'}},
              tags=['Server grants'], operation_id='issueServerGrant')
    def issue(request: ServerGrantRequest, caller=Depends(authenticate),
              idempotency_key: str | None = Header(default=None, max_length=128,
                                                   description='Caller-scoped retry key; conflicting payloads return 409.')):
        return invoke(access.issue, caller, str(request.server_id), request.public_key,
                      request.ttl_seconds, idempotency_key)

    @app.get('/v1/server-grants/{grant_id}', response_model=ServerGrantResponse,
             responses=errors, tags=['Server grants'], operation_id='getServerGrant')
    def get(grant_id: UUID, caller=Depends(authenticate)):
        return invoke(access.get, caller, str(grant_id))

    @app.delete('/v1/server-grants/{grant_id}', response_model=ServerGrantResponse,
                responses=errors, tags=['Server grants'], operation_id='revokeServerGrant',
                description='Idempotent. cleanup_pending means dependency failure delayed complete revocation.')
    def revoke(grant_id: UUID, caller=Depends(authenticate)):
        return invoke(access.revoke, caller, str(grant_id))

    return app


def configured_app(directory=Path('/run/server-api')):
    from latch_api.openstack import OpenStackNetwork
    from latch_common.warpgate import Api
    config = json.loads((directory / 'inventory.json').read_text())
    inventory = {name: Server(server_id=name, **server)
                 for name, server in config.get('servers', config.get('vms', {})).items()}
    network = OpenStackNetwork(json.loads((directory / 'openstack.json').read_text()))
    gateway = ServerGateway(Api('https://warpgate:8888', directory, control_plane_host='localhost'))
    access = ServerAccess('/data/grants.sqlite', inventory, config['permissions'], network, gateway,
                      config['gateway'])
    return create_app(access, (directory / 'api-token').read_text().strip())


def main():
    # Root prepares owner-only bind-mounted configuration, then drops privileges.
    directory = Path('/run/server-api')
    (directory / 'warpgate').mkdir(mode=0o700, parents=True, exist_ok=True)
    sources = {'/secrets/api-token': 'api-token', '/secrets/openstack.json': 'openstack.json',
               '/config/inventory.json': 'inventory.json',
               '/gateway/admin-password': 'warpgate/admin-password',
               '/gateway/tls.certificate.pem': 'warpgate/tls.certificate.pem'}
    for source, destination in sources.items():
        path = directory / destination
        shutil.copyfile(source, path)
        path.chmod(0o600)
        os.chown(path, 10001, 10001)
    for path in (directory / 'warpgate', directory, Path('/data')):
        os.chown(path, 10001, 10001)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
    import uvicorn
    uvicorn.run(configured_app(directory), host='0.0.0.0', port=8320, access_log=False)


if __name__ == '__main__':
    main()
