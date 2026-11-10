# Shared Python components

The `latch_common/` package contains dependency-free code used by the laptop CLI, guest tools,
and Latch API containers. It imports only the Python standard library so that
host commands and guest provisioning do not require API dependencies.

- `warpgate.Api`: cookie-authenticated Warpgate admin client, certificate trust,
  control-plane host selection, and one-time session renewal.
- `cloud_init.cloud_config`: platform cloud-init policy used by Nova vendor data
  and the static vendor-data CLI in `guest/target-cloud-init.py`.

Import these modules directly with `from latch_common...`. Deployment images and
guest sync must include this package alongside `latch_api` or `guest`; the laptop
CLI adds the repository root to Python's module search path at its entry point.
