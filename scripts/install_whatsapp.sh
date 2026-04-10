#!/usr/bin/env bash
# Install baileys-api (WhatsApp gateway) for RELAIS.
#
# Clones fazer-ai/baileys-api at a pinned commit to $RELAIS_HOME/vendor/baileys-api,
# runs bun install, and prints instructions for API key creation.
#
# Idempotent: skips clone if directory exists, verifies SHA matches.

set -euo pipefail

# ---- Pinned commit SHA ----
# IMPORTANT: Pin to a verified commit SHA before production use.
# Run: cd $RELAIS_HOME/vendor/baileys-api && git rev-parse HEAD
# Then replace "main" below with that SHA.
PINNED_SHA="main"

RELAIS_HOME="${RELAIS_HOME:-./.relais}"
VENDOR_DIR="${RELAIS_HOME}/vendor/baileys-api"

# ---- Prerequisites ----
if ! command -v bun &>/dev/null; then
    echo "ERROR: bun is not installed."
    echo "Install with: curl -fsSL https://bun.sh/install | bash"
    exit 1
fi

# ---- Clone or verify ----
if [ -d "$VENDOR_DIR" ]; then
    echo "baileys-api already installed at $VENDOR_DIR"
    cd "$VENDOR_DIR"
    CURRENT_SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    if [ "$PINNED_SHA" != "main" ] && [ "$CURRENT_SHA" != "$PINNED_SHA" ]; then
        echo "WARNING: Current SHA ($CURRENT_SHA) differs from pinned ($PINNED_SHA)"
        echo "Run: cd $VENDOR_DIR && git checkout $PINNED_SHA"
    fi
else
    echo "Cloning baileys-api to $VENDOR_DIR..."
    mkdir -p "$(dirname "$VENDOR_DIR")"
    git clone https://github.com/fazer-ai/baileys-api.git "$VENDOR_DIR"
    cd "$VENDOR_DIR"
    if [ "$PINNED_SHA" != "main" ]; then
        git checkout "$PINNED_SHA"
    fi
fi

# ---- Install dependencies ----
echo "Running bun install..."
cd "$VENDOR_DIR"
bun install

echo ""
echo "========================================"
echo " baileys-api installed successfully!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Create an API key (skip in NODE_ENV=development):"
echo "     cd $VENDOR_DIR"
echo "     bun scripts/manage-api-keys.ts create user relais-adapter"
echo "     # Store the output as WHATSAPP_API_KEY in .env"
echo ""
echo "  2. Set environment variables in .env:"
echo "     WHATSAPP_PHONE_NUMBER=+33XXXXXXXXX"
echo "     WHATSAPP_API_KEY=<key from step 1>"
echo "     WHATSAPP_WEBHOOK_SECRET=<random string, min 6 chars>"
echo ""
echo "  3. Enable WhatsApp in ~/.relais/config/aiguilleur.yaml:"
echo "     whatsapp:"
echo "       enabled: true"
echo ""
echo "  4. Start baileys-api:"
echo "     supervisorctl start optional:baileys-api"
echo ""
echo "  5. Pair via /settings whatsapp on any channel"
