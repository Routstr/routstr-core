#!/bin/bash

# Sync events from external relays to your local strfry instance

# Configuration
STRFRY_CMD="docker compose exec relay strfry"

# Event kinds from your original config
# EVENT_KINDS="[7374, 7375, 7376, 9321, 17375, 10019, 38000, 38172, 38421]"
# Or use empty filter to sync everything
FILTER='{}'

# Sync from relay.damus.io
echo "Syncing from relay.damus.io..."
$STRFRY_CMD sync wss://relay.damus.io --filter "$FILTER"

# You can also stream events in real-time
# Uncomment to enable continuous streaming:
# echo "Starting continuous stream from relay.damus.io..."
# $STRFRY_CMD stream wss://relay.damus.io &

# To sync specific event kinds, use:
# $STRFRY_CMD sync wss://relay.damus.io --filter '{"kinds":[7374, 7375, 7376, 17375, 10019, 9321]}'

# To sync events from specific pubkeys:
# PUBKEYS='["8bf629b3d519a0f8a8390137a445c0eb2f5f2b4a8ed71151de898051e8006f13"]'
# $STRFRY_CMD sync wss://relay.damus.io --filter "{\"authors\":$PUBKEYS}" 