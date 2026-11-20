"""Entry point: python -m app [serve|hash-password|revoke-tokens]"""

import os
import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "hash-password":
        from .security import hash_password_cli

        hash_password_cli()
    elif cmd == "revoke-tokens":
        from . import config
        from .auth import OwnerOAuthProvider

        n = OwnerOAuthProvider(config.load()).revoke_all()
        print(f"Revoked {n} tokens. All MCP clients must log in again.")
    elif cmd == "serve":
        import uvicorn

        uvicorn.run(
            "app.server:app",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
            proxy_headers=True,
            forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
            lifespan="on",
            access_log=os.environ.get("ACCESS_LOG", "false").lower() == "true",
            timeout_keep_alive=30,
        )
    else:
        sys.exit(f"unknown command {cmd!r}; use serve | hash-password | revoke-tokens")


if __name__ == "__main__":
    main()
