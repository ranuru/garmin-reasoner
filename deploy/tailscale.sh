#!/usr/bin/env sh
# Expose the app (listening on 127.0.0.1:8000) through the host's Tailscale.
#
# - Funnel (public internet): only the paths claude.ai must reach, on FUNNEL_PORT.
# - Serve (tailnet only): admin routes, on ADMIN_PORT. Never put these on Funnel.
#
# Funnel allows ports 443, 8443 and 10000. If something else on this machine already
# uses Funnel on 443 (e.g. Home Assistant), give this app its own port: sharing one
# origin lets the other app answer OAuth discovery (/.well-known/...) and claude.ai
# ends up at the wrong login page. With a non-443 port, PUBLIC_BASE_URL must include
# it, e.g. https://host.tailnet.ts.net:10000.
#
#   FUNNEL_PORT=10000 sh deploy/tailscale.sh
#
# The config persists across reboots. Check it with `tailscale funnel status`, and see
# `tailscale funnel --help` if your Tailscale version's flags differ.
set -eu

TARGET="http://127.0.0.1:8000"
FUNNEL_PORT="${FUNNEL_PORT:-10000}"
ADMIN_PORT="${ADMIN_PORT:-9443}"

PUBLIC_PATHS="
/mcp
/mcp-login
/authorize
/token
/register
/revoke
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource
"

ADMIN_PATHS="
/admin
"

for path in $PUBLIC_PATHS; do
    tailscale funnel --bg --https="$FUNNEL_PORT" --set-path="$path" "$TARGET$path"
done

for path in $ADMIN_PATHS; do
    tailscale serve --bg --https="$ADMIN_PORT" --set-path="$path" "$TARGET$path"
done

tailscale funnel status
