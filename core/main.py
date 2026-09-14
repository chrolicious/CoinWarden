from core.config import Config
from core.api_client import BattleNetClient


def main():
    config = Config()
    client = BattleNetClient(config)

    seen_connected_realm_ids = set()
    for realm_slug in config.realm_slugs:
        connected_realm_id = client.get_connected_realm_id(realm_slug)
        print(f"{realm_slug}: connected realm ID {connected_realm_id}")

        if connected_realm_id in seen_connected_realm_ids:
            print("  (already scanned as part of another realm in this cluster)")
            continue
        seen_connected_realm_ids.add(connected_realm_id)

        auctions = client.get_auctions_for_connected_realm(connected_realm_id)
        print(f"  fetched {len(auctions.get('auctions', []))} non-commodity auction listings")


if __name__ == "__main__":
    main()
