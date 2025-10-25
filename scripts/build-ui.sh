#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UI_DIR="$PROJECT_ROOT/ui"

echo "Building Routstr UI for static deployment..."
echo "UI directory: $UI_DIR"

if [ ! -d "$UI_DIR" ]; then
    echo "Error: UI directory not found at $UI_DIR"
    exit 1
fi

cd "$UI_DIR"

echo "Installing dependencies..."
if command -v pnpm &> /dev/null; then
    pnpm install
elif command -v npm &> /dev/null; then
    npm install
else
    echo "Error: Neither pnpm nor npm found. Please install Node.js and npm."
    exit 1
fi

# Check for root .env file (centralized configuration)
ROOT_ENV_FILE="$PROJECT_ROOT/.env"
UI_ENV_FILE="$UI_DIR/.env.local"

if [ -f "$ROOT_ENV_FILE" ]; then
    echo "Loading environment variables from $ROOT_ENV_FILE"
    # Extract NEXT_PUBLIC_ variables and create .env.local for Next.js
    grep '^NEXT_PUBLIC_' "$ROOT_ENV_FILE" > "$UI_ENV_FILE"
    echo "Created $UI_ENV_FILE with UI configuration"
else
    echo "Warning: .env file not found in project root. Using default configuration."
    echo "Create a .env file based on .env.example for proper configuration."
    # Create empty .env.local to avoid issues
    > "$UI_ENV_FILE"
fi

echo "Building static export..."
if command -v pnpm &> /dev/null; then
    pnpm run build
else
    npm run build
fi

mkdir ../ui_out
mv out/* ../ui_out

# Clean up the temporary .env.local file
if [ -f "$UI_ENV_FILE" ]; then
    rm "$UI_ENV_FILE"
    echo "Cleaned up temporary $UI_ENV_FILE"
fi

echo ""
echo "✓ UI build complete!"
echo "Static files generated at: $UI_DIR/out"
echo ""
echo "To serve the UI from the Python backend:"
echo "  1. Configure NEXT_PUBLIC_API_URL in the root .env file"
echo "  2. For development: Set NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 or leave empty for relative paths"
echo "  3. For production: Set NEXT_PUBLIC_API_URL=https://your-production-api.com"
echo "  4. Start the backend: uvicorn routstr.core.main:app --host 0.0.0.0 --port 8000"
echo "  5. Access the UI at: http://localhost:8000"
echo ""

