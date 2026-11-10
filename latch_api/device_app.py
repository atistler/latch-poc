"""Optional authenticated device API; isolated from Nova vendor-data delivery."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
import os
from pathlib import Path
import shutil

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from latch_api.device_access import AccessError, Device, DeviceAccess, OpenBaoLDAP, WarpgateGateway
from latch_api.documentation import (DeviceGrantResponse, LivenessResponse, RequestErrorResponse,
                                     error_responses, install_documentation)

logger = logging.getLogger('uvicorn.error')


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    device_id: str = Field(min_length=1, max_length=80)
    role: str = Field(pattern='^(readonly|operator|admin)$')
    public_key: str = Field(min_length=20, max_length=16384)
    ttl_seconds: int = Field(default=300, ge=1, le=300)


def create_app(access, token, principal='lab-operator', sweep_interval=2):
    if len(token) < 32 or not principal:
        raise ValueError('Configure an owner-only POC API token of at least 32 characters')

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(access.sweep)

        async def expire():
            while True:
                await asyncio.sleep(sweep_interval)
                try:
                    await asyncio.to_thread(access.sweep)
                except Exception:
                    logger.error('Device grant reconciliation failed; retry scheduled')

        task = asyncio.create_task(expire())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title='Latch device access API', version='1.0.0', docs_url=None,
                  redoc_url='/docs', lifespan=lifespan,
                  description=('Short-lived agentless device SSH grants. The bearer token '
                               'maps to a server-controlled caller; credentials are injected '
                               'by Warpgate and are never returned to the client.'))
    install_documentation(app)
    bearer = HTTPBearer(auto_error=False, scheme_name='LatchBearer',
                        description='Owner-only POC API token; caller identity is configured server-side.')

    def authenticate(authorization: str | None = Header(default=None, include_in_schema=False),
                     credential=Depends(bearer)):
        expected = 'Bearer ' + token
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, 'Authentication required', headers={'WWW-Authenticate': 'Bearer'})
        return principal

    def invoke(operation, *args):
        try:
            return operation(*args)
        except AccessError as error:
            raise HTTPException(error.status_code, str(error)) from None

    @app.get('/health/live', operation_id='deviceLiveness', tags=['Device health'],
             responses={200: {'model': LivenessResponse}})
    def live():
        return {'status': 'alive'}

    @app.post('/v1/device-grants', status_code=201, operation_id='issueDeviceGrant',
              tags=['Device grants'], summary='Create a temporary device SSH grant',
              description=('Authorizes the device and role, leases a temporary LDAP identity '
                           'and configures isolated Warpgate admission. TTL is 1–300 seconds. '
                           'The response contains a connection selector, never its password.'),
              responses={201: {'model': DeviceGrantResponse},
                         422: {'model': RequestErrorResponse,
                               'description': 'Invalid request, TTL or SSH public key.'}, **error_responses(**{
                  '401': 'Bearer authentication required.', '403': 'Device or role not permitted.',
                  '503': 'Provisioning failed; compensating cleanup is scheduled.'})})
    def issue(request: GrantRequest, caller=Depends(authenticate)):
        return invoke(access.issue, caller, request.device_id, request.role,
                      request.public_key, request.ttl_seconds)

    @app.get('/v1/device-grants/{grant_id}', operation_id='getDeviceGrant',
             tags=['Device grants'], summary='Read an owned grant and reconcile its expiry',
             responses={200: {'model': DeviceGrantResponse}, **error_responses(**{
                 '401': 'Bearer authentication required.', '404': 'Grant absent or owned by another caller.',
                 '503': 'Grant reconciliation unavailable.'})})
    def get(grant_id: str, caller=Depends(authenticate)):
        return invoke(access.get, caller, grant_id)

    @app.delete('/v1/device-grants/{grant_id}', operation_id='revokeDeviceGrant',
                tags=['Device grants'], summary='Revoke an owned grant and close its sessions',
                description=('Repeated revocation is safe. A cleanup_pending response means '
                             'a dependency prevented complete revocation; reconciliation retries.'),
                responses={200: {'model': DeviceGrantResponse}, **error_responses(**{
                    '401': 'Bearer authentication required.', '404': 'Grant absent or owned by another caller.',
                    '503': 'Grant reconciliation unavailable.'})})
    def revoke(grant_id: str, caller=Depends(authenticate)):
        return invoke(access.revoke, caller, grant_id)

    return app


def configured_app(directory=Path('/run/device-api')):
    from latch_common.warpgate import Api
    config = json.loads((directory / 'inventory.json').read_text())
    inventory = {name: Device(**device) for name, device in config['devices'].items()}
    bao = OpenBaoLDAP('http://ldap-openbao:8200', directory / 'device-bao-token')
    gateway = WarpgateGateway(Api('https://warpgate:8888', directory,
                                  control_plane_host='localhost'))
    access = DeviceAccess('/data/grants.sqlite', inventory, config['permissions'], bao, gateway)
    return create_app(access, (directory / 'api-token').read_text().strip())


def main():
    # Root only prepares private files from owner-only host bind mounts. Requests
    # and the expiry worker run as the same unprivileged UID as the vendor API.
    directory = Path('/run/device-api')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / 'warpgate').mkdir(mode=0o700, exist_ok=True)
    sources = {
        '/secrets/api-token': 'api-token',
        '/secrets/device-bao-token': 'device-bao-token',
        '/config/inventory.json': 'inventory.json',
        '/gateway/admin-password': 'warpgate/admin-password',
        '/gateway/tls.certificate.pem': 'warpgate/tls.certificate.pem',
    }
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
    uvicorn.run(configured_app(directory), host='0.0.0.0', port=8310, access_log=False)


if __name__ == '__main__':
    main()
