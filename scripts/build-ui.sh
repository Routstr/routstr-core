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

echo "Building static export..."
if command -v pnpm &> /dev/null; then
    pnpm run build
else
    npm run build
fi

mv out ../ui_out

echo ""
echo "✓ UI build complete!"
echo "Static files generated at: $UI_DIR/out"
echo ""
echo "To serve the UI from the Python backend:"
echo "  1. Make sure NEXT_PUBLIC_API_URL is not set (or set to empty) to use relative API paths"
echo "  2. Start the backend: uvicorn routstr.core.main:app --host 0.0.0.0 --port 8000"
echo "  3. Access the UI at: http://localhost:8000"
echo ""

