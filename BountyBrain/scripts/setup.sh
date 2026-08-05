#!/usr/bin/env bash
set -euo pipefail

echo "=== BountyBrain Setup ==="

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}' | cut -d. -f1,2)
REQUIRED="3.11"
if python3 -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)"; then
    echo "✓ Python $PYTHON_VERSION"
else
    echo "✗ Python 3.11+ required (got $PYTHON_VERSION)"
    exit 1
fi

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate
source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet -e .

# Copy .env if not exists
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "Created .env — edit it with your API keys:"
    echo "  GITHUB_TOKEN      → https://github.com/settings/tokens"
    echo "  ANTHROPIC_API_KEY → https://console.anthropic.com/"
    echo "  ALGORA_API_KEY    → https://console.algora.io/"
fi

# Create storage directories
mkdir -p storage/{datasets,logs,db,workspaces,models}

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. source .venv/bin/activate"
echo "  3. bountybrain run"
echo "  4. bountybrain dashboard  # start web UI on :8080"
