#!/usr/bin/env sh
# Expose the app (listening on 127.0.0.1:8000) through the host's Tailscale.
#
# - Funnel (public internet, port 443): the app under a path prefix, next to whatever
#   owns "/" on this host (Home Assistant). claude.ai only connects on 443.
# - Serve (tailnet only, ADMIN_PORT): admin routes. Never put these on Funnel.
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
ADMIN_PORT="${ADMIN_PORT:-8443}"

for path in \
    "$PREFIX" \
    "/.well-known/oauth-authorization-server$PREFIX" \
    "/.well-known/oauth-protected-resource$PREFIX"; do
    tailscale funnel --bg --https=443 --set-path="$path" "$TARGET$path"
done

tailscale serve --bg --https="$ADMIN_PORT" --set-path=/admin "$TARGET/admin"

tailscale funnel status
