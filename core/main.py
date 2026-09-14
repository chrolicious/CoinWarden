from core.config import Config
from core.api_client import BattleNetClient


def main():
    config = Config()
    client = BattleNetClient(config)

    connected_realm_id = client.get_connected_realm_id(config.realm_slug)
    print(f"Connected realm ID for {config.realm_slug}: {connected_realm_id}")

    auctions = client.get_auctions_for_connected_realm(connected_realm_id)
    print(f"Fetched {len(auctions.get('auctions', []))} non-commodity auction listings")


if __name__ == "__main__":
    main()
