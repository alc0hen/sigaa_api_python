import argparse
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .auth import CLIENTS_FILE, list_clients, register_client, revoke_client


def cmd_generate(name):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_hex = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    public_hex = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()

    register_client(public_hex, name)
    print(f"Registered client '{name}' in {CLIENTS_FILE}")
    print(f"  public key  (goes in the request URL):      {public_hex}")
    print(f"  private key (client keeps this, signs with it): {private_hex}")
    print("\nThe private key above is shown only once — copy it now.")


def cmd_list():
    clients = list_clients()
    if not clients:
        print("No authorized clients registered.")
        return
    for public_key, meta in clients.items():
        print(f"{public_key}  name={meta.get('name')!r}  added_at={meta.get('added_at')}")


def cmd_revoke(public_key_hex):
    if revoke_client(public_key_hex):
        print(f"Revoked {public_key_hex}")
    else:
        print(f"No such client: {public_key_hex}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate a keypair and register the public key.")
    generate_parser.add_argument("--name", required=True, help="Human-readable name for the client server.")

    subparsers.add_parser("list", help="List authorized client public keys.")

    revoke_parser = subparsers.add_parser("revoke", help="Revoke a client's authorization.")
    revoke_parser.add_argument("public_key_hex")

    args = parser.parse_args()
    if args.command == "generate":
        cmd_generate(args.name)
    elif args.command == "list":
        cmd_list()
    elif args.command == "revoke":
        cmd_revoke(args.public_key_hex)


if __name__ == "__main__":
    main()
