#!/bin/bash
set -e

echo "Setting up streamrip-web-gui with uv..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "Please restart your shell or run: source $HOME/.cargo/env"
    exit 0
fi

# Create venv and install
uv venv
uv pip install -e .

echo ""
echo "Done! To run:"
echo "  source .venv/bin/activate"
echo "  python app.py"
