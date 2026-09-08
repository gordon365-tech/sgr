#!/usr/bin/env python3
"""
Setzt/entfernt is_admin fuer einen User per Email.

Kontext: is_admin existierte bisher nur als transienter JWT-Claim
(hartcodiert False in allen Aufrufstellen von create_access_token) -
es gab keinen Weg, einen User tatsaechlich zum Admin zu machen, obwohl
mehrere Endpoints (z.B. POST /api/v1/risk/kill-switch/reset) require_admin
verlangen (siehe Migration alembic/versions/0004_user_is_admin.py und
sgr.core.database.UserModel.is_admin Docstring fuer die vollstaendige
Begruendung).

Bewusst ein CLI-Skript, KEIN API-Endpoint: Admin-Rechte per HTTP-Request
vergeben zu koennen waere ein Sicherheitsrisiko (Privilege-Escalation-
Weg). Dieses Skript setzt Server-/DB-Zugriff voraus - genau die
Zugriffsebene, die fuer eine Aktion dieser Tragweite angemessen ist.

Usage (im sgr-api oder sgr-worker Container, wo die App-Konfiguration
und DB-Erreichbarkeit vorhanden sind):

    docker compose -f docker/docker-compose.prod.yml exec api \\
        python scripts/grant_admin.py gordon@sgr-trading.example

    docker compose -f docker/docker-compose.prod.yml exec api \\
        python scripts/grant_admin.py gordon@sgr-trading.example --revoke

Der User muss bereits existieren (z.B. via POST /api/v1/auth/register).
Nach dem Setzen muss sich der User neu einloggen (POST /api/v1/auth/login)
- der neue is_admin-Claim wird erst beim naechsten Token-Erstellen
wirksam, bestehende Access Tokens tragen weiterhin den alten Wert bis
sie ablaufen (siehe create_access_token, 60 Min TTL).
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def _main(email: str, revoke: bool) -> int:
    from sgr.core.database import close_db, init_db
    from sgr.core.repositories import get_repositories

    # init_db() ist idempotent (CREATE TABLE IF NOT EXISTS /
    # CREATE EXTENSION IF NOT EXISTS) - unschaedlich gegen eine bereits
    # laufende Produktions-DB, aber notwendig, damit get_session() im
    # Repository-Layer ueberhaupt eine Session-Factory hat.
    await init_db()
    try:
        repos = get_repositories()
        updated = await repos.users.set_admin_status(email, is_admin=not revoke)
        if not updated:
            print(f"No user found with email {email!r}.", file=sys.stderr)
            return 1

        action = "revoked from" if revoke else "granted to"
        print(f"Admin access {action} {email}.")
        print("The user must log in again for this to take effect.")
        return 0
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="Email address of the user")
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Revoke admin access instead of granting it",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(_main(args.email, args.revoke))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
