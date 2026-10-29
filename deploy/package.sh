#!/usr/bin/env bash
set -euo pipefail

# Create the distribution directory
mkdir -p dist

# Package only the source directory for Lambda deployment
# Run from the project root to ensure correct relative paths
cd "$(dirname "$0")"/.. && rm -f dist/secondlook.zip
zip -r dist/secondlook.zip src/ -x "*/__pycache__/*" "*.pyc" > /dev/null

# Verify the zip was created
if [[ ! -f dist/secondlook.zip ]]; then
    echo "Error: dist/secondlook.zip was not created" >&2
    exit 1
fi

echo "Packaging complete: dist/secondlook.zip"
