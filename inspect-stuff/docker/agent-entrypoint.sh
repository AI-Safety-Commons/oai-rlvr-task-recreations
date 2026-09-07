#!/bin/sh
set -eu

# Docker creates this marker at container creation, after image build.
# Keep the ordinary rollout workspace free of this incidental runtime label.
# Do not mask procfs, mounts, or other kernel interfaces.
rm -f /.dockerenv

# Trust the private CA used by the provider-compatible HTTPS endpoints.
update-ca-certificates >/dev/null

exec "$@"
