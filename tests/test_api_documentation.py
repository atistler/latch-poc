"""Public API docs must describe real operations without touching dependencies."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

AVAILABLE = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'cryptography'))
if AVAILABLE:
    from fastapi.testclient import TestClient
    from latch_api.app import create_app as vendor_app
    from latch_api.device_app import create_app as device_app
    from latch_api.server_app import create_app as server_app
    from latch_api.documentation import yaml_text


@unittest.skipUnless(AVAILABLE, 'Install latch_api/requirements.txt')
class APIDocumentation(unittest.TestCase):
    def apps(self):
        return (vendor_app(bao=Mock()),
                device_app(Mock(), 'documentation-test-token-' + 'x' * 32),
                server_app(Mock(), 'documentation-test-token-' + 'x' * 32))

    def test_redoc_and_both_schema_formats_are_public_and_side_effect_free(self):
        for app in self.apps():
            with self.subTest(app=app.title):
                # No lifespan: documentation must not require OpenBao or LDAP.
                client = TestClient(app)
                docs = client.get('/docs')
                self.assertEqual(docs.status_code, 200)
                self.assertIn('<redoc spec-url="/openapi.json">', docs.text)
                self.assertNotIn('SwaggerUIBundle', docs.text)
                schema = client.get('/openapi.json')
                self.assertEqual(schema.status_code, 200)
                download = client.get('/openapi.yaml')
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.text, yaml_text(schema.json()))
                self.assertTrue(download.headers['content-type'].startswith('application/yaml'))
                self.assertNotIn('/docs', schema.json()['paths'])
                self.assertNotIn('/openapi.yaml', schema.json()['paths'])

    def test_grant_security_and_successful_response_shapes_are_documented(self):
        for app, model in zip(self.apps()[1:], ('DeviceGrantResponse', 'ServerGrantResponse')):
            schema = app.openapi()
            security = schema['components']['securitySchemes']['LatchBearer']
            self.assertEqual((security['type'], security['scheme']), ('http', 'bearer'))
            for path, methods in schema['paths'].items():
                for method, operation in methods.items():
                    if path.startswith('/v1/'):
                        self.assertEqual(operation['security'], [{'LatchBearer': []}])
                        self.assertIn('401', operation['responses'])
                        code = '201' if method == 'post' else '200'
                        self.assertIn(model, str(operation['responses'][code]))
                        self.assertFalse(any(p['in'] == 'header' and p['name'].lower() == 'authorization'
                                             for p in operation.get('parameters', [])))
                    else:
                        self.assertNotIn('security', operation)
            self.assertNotIn('documentation-test-token-', str(schema))

    def test_vendor_data_remains_a_string_and_has_no_bearer_scheme(self):
        schema = self.apps()[0].openapi()
        operation = schema['paths']['/v1/vendor-data']['post']
        self.assertEqual(operation['responses']['200']['content']['application/json']['schema']['type'], 'string')
        self.assertIn('503', operation['responses'])
        self.assertNotIn('securitySchemes', schema['components'])

    def test_checked_in_contract_has_not_drifted(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, 'scripts/export_openapi.py', '--check'], cwd=root, check=True)

    def test_combined_contract_references_resolve_and_operations_identify_their_service(self):
        from scripts.export_openapi import combined_schema
        schema = combined_schema()

        def check(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    reference = value['$ref']
                    self.assertTrue(reference.startswith('#/'))
                    resolved = schema
                    for part in reference[2:].split('/'):
                        resolved = resolved[part.replace('~1', '/').replace('~0', '~')]
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(schema)
        operation_ids = []
        for methods in schema['paths'].values():
            for operation in methods.values():
                self.assertTrue(operation['servers'])
                operation_ids.append(operation['operationId'])
        self.assertEqual(len(operation_ids), len(set(operation_ids)))


if __name__ == '__main__':
    unittest.main()
