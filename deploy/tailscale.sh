#!/usr/bin/env sh
# Expose the app (listening on 127.0.0.1:8000) through the host's Tailscale.
#
# - Funnel (public internet, port 443): only the paths claude.ai must reach.
# - Serve (tailnet only, port 8443): admin routes. Never put these on Funnel.
#
# Run once on the home server; the config persists across reboots. Check the result
# with `tailscale funnel status` and `tailscale serve status`, and see
# `tailscale funnel --help` if your Tailscale version's flags differ.
set -eu

TARGET="http://127.0.0.1:8000"

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
    tailscale funnel --bg --https=443 --set-path="$path" "$TARGET$path"
done

for path in $ADMIN_PATHS; do
    tailscale serve --bg --https=8443 --set-path="$path" "$TARGET$path"
done

tailscale funnel status
