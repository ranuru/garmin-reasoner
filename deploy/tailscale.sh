#!/usr/bin/env sh
# Expose the app (listening on 127.0.0.1:8000) to the internet with Tailscale Funnel.
# One option among several: any HTTPS reverse proxy or tunnel works (see README.md).
#
# Funnel (public internet, port 443) serves the app under a path prefix, next to
# whatever may own "/" on this host. claude.ai only connects on 443. The app itself
# only answers the MCP and OAuth routes under the prefix (PUBLIC_PATHS in mcp_auth.py).
#
# PREFIX must match the path of PUBLIC_BASE_URL, e.g.
#   PUBLIC_BASE_URL=https://host.tailnet.ts.net/garmin  ->  PREFIX=/garmin (the default)
#
# Besides /garmin itself, two OAuth discovery paths outside the prefix are needed:
# RFC 8414/9728 put a path-prefixed server's metadata at /.well-known/<name>/garmin...
# These don't collide with the other app's own /.well-known/oauth-authorization-server.
#
# The config persists across reboots. Check it with `tailscale funnel status`, and see
# `tailscale funnel --help` if your Tailscale version's flags differ.
set -eu

TARGET="http://127.0.0.1:8000"
PREFIX="${PREFIX:-/garmin}"

for path in \
    "$PREFIX" \
    "/.well-known/oauth-authorization-server$PREFIX" \
    "/.well-known/oauth-protected-resource$PREFIX"; do
    tailscale funnel --bg --https=443 --set-path="$path" "$TARGET$path"
done

tailscale funnel status
