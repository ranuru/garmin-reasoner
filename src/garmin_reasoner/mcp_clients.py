"""Manage MCP OAuth clients by hand, for connectors that don't register themselves.

claude.ai's "Add custom connector" dialog accepts an OAuth Client ID and Secret under
Advanced settings. Create them here, on the server:

    docker compose exec garmin-reasoner python -m garmin_reasoner.mcp_clients create
    docker compose exec garmin-reasoner python -m garmin_reasoner.mcp_clients list
    docker compose exec garmin-reasoner python -m garmin_reasoner.mcp_clients delete <client_id>

The secret is printed once. Deleting a client also revokes every token issued to it.
Clients are limited to the redirect URIs in MCP_ALLOWED_REDIRECT_URIS, like
self-registered ones.
"""

import argparse
import asyncio
import json
import secrets
import sys
import time
from contextlib import closing

from mcp.shared.auth import OAuthClientInformationFull

from garmin_reasoner import db
from garmin_reasoner.config import Settings, get_settings
from garmin_reasoner.mcp_auth import SCOPE, SQLiteOAuthProvider


def create_client(settings: Settings, name: str) -> OAuthClientInformationFull:
    client = OAuthClientInformationFull(
        client_id=f"garmin-reasoner-{secrets.token_urlsafe(12)}",
        client_secret=secrets.token_urlsafe(32),
        client_id_issued_at=int(time.time()),
        client_secret_expires_at=None,
        client_name=name,
        redirect_uris=settings.mcp_allowed_redirect_uris,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=SCOPE,
        # HTTP Basic works too; see mcp_auth.BasicAuthForPostClients.
        token_endpoint_auth_method="client_secret_post",
    )
    provider = SQLiteOAuthProvider(
        settings.database_path, settings.public_base_url, settings.mcp_allowed_redirect_uris
    )
    asyncio.run(provider.register_client(client))
    return client


def list_clients(settings: Settings) -> list[dict[str, object]]:
    with closing(db.connect(settings.database_path)) as conn:
        rows = conn.execute("SELECT info_json FROM mcp_clients ORDER BY created_at").fetchall()
    clients = []
    for row in rows:
        info = json.loads(row["info_json"])
        clients.append(
            {
                "client_id": info["client_id"],
                "name": info.get("client_name"),
                "auth": info.get("token_endpoint_auth_method"),
                "redirect_uris": info.get("redirect_uris"),
            }
        )
    return clients


def delete_client(settings: Settings, client_id: str) -> bool:
    with closing(db.connect(settings.database_path)) as conn, conn:
        # Pending requests, codes and tokens cascade (see migration 4).
        return (
            conn.execute("DELETE FROM mcp_clients WHERE client_id = ?", (client_id,)).rowcount > 0
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m garmin_reasoner.mcp_clients")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a client and print its ID and secret")
    create.add_argument("--name", default="Claude")
    commands.add_parser("list", help="list clients (secrets are not shown)")
    delete = commands.add_parser("delete", help="delete a client and revoke its tokens")
    delete.add_argument("client_id")
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = db.connect(settings.database_path)
    db.init_db(conn)
    conn.close()

    if args.command == "create":
        client = create_client(settings, args.name)
        print("Paste these into the connector's Advanced settings. The secret is shown once.")
        print(f"OAuth Client ID:     {client.client_id}")
        print(f"OAuth Client Secret: {client.client_secret}")
    elif args.command == "list":
        for client in list_clients(settings):
            print(json.dumps(client))
    elif args.command == "delete":
        if not delete_client(settings, args.client_id):
            print(f"no client {args.client_id}", file=sys.stderr)
            return 1
        print(f"deleted {args.client_id} and revoked its tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
