#!/bin/bash

# Script to set up a local Cashu mint instance for integration testing

echo "Setting up local Cashu mint instance..."

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Error: Docker is not installed. Please install Docker first."
    exit 1
fi

# Stop any existing mint container
echo "Stopping any existing Cashu mint container..."
docker stop cashu-mint-test 2>/dev/null || true
docker rm cashu-mint-test 2>/dev/null || true

# Start Cashu mint container
echo "Starting Cashu mint container..."
docker run -d \
    --name cashu-mint-test \
    -p 3338:3338 \
    -e MINT_BACKEND_BOLT11_SAT=FakeWallet \
    -e MINT_LISTEN_HOST=0.0.0.0 \
    -e MINT_LISTEN_PORT=3338 \
    -e MINT_PRIVATE_KEY="$(openssl rand -hex 32)" \
    cashubtc/nutshell:latest \
    python -m cashu.mint

# Wait for mint to be ready
echo "Waiting for Cashu mint to be ready..."
for i in {1..30}; do
    if curl -f http://localhost:3338/v1/info >/dev/null 2>&1; then
        echo "Cashu mint is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "Error: Cashu mint failed to start within 30 seconds"
        docker logs cashu-mint-test
        exit 1
    fi
    sleep 1
done

# Display connection info
echo ""
echo "Cashu mint is running at: http://localhost:3338"
echo ""
echo "To run integration tests with real Cashu mint:"
echo "  export USE_REAL_MINT=true"
echo "  export MINT_URL=http://localhost:3338"
echo "  pytest tests/integration/ -v"
echo ""
echo "To stop Cashu mint:"
echo "  docker stop cashu-mint-test"
echo "  docker rm cashu-mint-test"