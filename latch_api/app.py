"""Latch API: Nova vendor data is the application's first capability."""
import json
import logging
import os
import time
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from latch_api.openbao import CAUnavailable, OpenBaoClient
from latch_api.profiles import render
from latch_api.documentation import (LivenessResponse, ReadinessResponse,
                                     error_responses, install_documentation)

logger = logging.getLogger('uvicorn.error')


class NovaContext(BaseModel):
    model_config = ConfigDict(extra='ignore')
    instance_id: UUID = Field(alias='instance-id')
    project_id: UUID = Field(alias='project-id')
    image_id: str | None = Field(default=None, alias='image-id')
    hostname: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    user_data: str | None = Field(default=None, alias='user-data')


def create_app(bao=None, profile_revision=None):
    app = FastAPI(title='Latch vendor-data API', version='1.0.0', docs_url=None,
                  redoc_url='/docs', description=(
                      'Nova dynamic vendor-data delivery. Returns public SSH CA trust '
                      'configuration; this service has no signing credentials. '
                      'Restrict its listener to the service-management network.'))
    install_documentation(app)
    client = bao or OpenBaoClient(os.environ.get('OPENBAO_ADDR', 'http://openbao:8200'))
    revision = profile_revision or os.environ.get('LATCH_PROFILE_REVISION', 'openbao-v1')
    # Reject invalid operator configuration during application startup.
    render('', '', revision)

    @app.get('/health/live', operation_id='vendorLiveness', tags=['Vendor-data health'],
             responses={200: {'model': LivenessResponse}})
    async def live():
        return {'status': 'alive'}

    @app.get('/health/ready', operation_id='vendorReadiness', tags=['Vendor-data health'],
             responses={200: {'model': ReadinessResponse},
                        **error_responses(**{'503': 'The SSH CA is unavailable or invalid.'})})
    async def ready():
        try:
            _, fingerprint = await client.public_ca()
        except CAUnavailable:
            raise HTTPException(status_code=503, detail='SSH CA unavailable') from None
        return {'status': 'ready', 'profile_revision': revision, 'ssh_ca_fingerprint': fingerprint}

    @app.post('/v1/vendor-data', response_model=str, operation_id='getVendorData',
              tags=['Vendor data'], summary='Render Nova cloud-init vendor data',
              description=('Nova supplies instance context. The response is a JSON string '
                           'containing cloud-config, not a JSON object. Tenant metadata '
                           'cannot override the trusted CA or platform SSH policy.'),
              responses=error_responses(**{'503': 'The SSH CA is unavailable or invalid.'}))
    async def vendor_data(context: NovaContext):
        started = time.monotonic()
        fields = {'instance_id': str(context.instance_id), 'profile_revision': revision}
        try:
            ca, fingerprint = await client.public_ca()
        except CAUnavailable:
            fields.update(outcome='ca_unavailable', latency_ms=round((time.monotonic() - started) * 1000))
            logger.warning(json.dumps(fields))
            raise HTTPException(status_code=503, detail='SSH CA unavailable') from None
        fields.update(outcome='served', ssh_ca_fingerprint=fingerprint,
                      latency_ms=round((time.monotonic() - started) * 1000))
        logger.info(json.dumps(fields))
        # Nova adds the cloud-init target name. Return a JSON string, not an object.
        return render(ca, fingerprint, revision)

    return app


app = create_app()
