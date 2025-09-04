#!/bin/bash

# Build script for MkDocs documentation

echo "Building Routstr Core documentation..."

# Check if mkdocs is installed
if ! command -v mkdocs &> /dev/null; then
    echo "MkDocs not found. Installing dependencies..."
    pip install -r docs/requirements.txt
fi

# Build the documentation
echo "Building docs..."
mkdocs build

# Serve locally for preview (optional)
if [ "$1" = "serve" ]; then
    echo "Starting documentation server at http://localhost:8001"
    mkdocs serve -a localhost:8001
elif [ "$1" = "deploy" ]; then
    echo "Deploying to GitHub Pages..."
    mkdocs gh-deploy --force
else
    echo "Documentation built successfully in ./site/"
    echo "Run './build-docs.sh serve' to preview locally"
    echo "Run './build-docs.sh deploy' to deploy to GitHub Pages"
fi