#!/bin/bash
# Railway startup script with detailed logging

set -e

echo "========================================="
echo "RAILWAY STARTUP DEBUG"
echo "========================================="
echo "Timestamp: $(date)"
echo "Working directory: $(pwd)"
echo "User: $(whoami)"
echo ""
echo "Environment variables:"
echo "  PORT: ${PORT:-NOT SET}"
echo "  PYTHONPATH: ${PYTHONPATH:-NOT SET}"
echo "  PLAYWRIGHT_BROWSERS_PATH: ${PLAYWRIGHT_BROWSERS_PATH:-NOT SET}"
echo ""

# Create credentials.json from environment variable if set
if [ -n "$GOOGLE_OAUTH_CREDENTIALS_JSON" ]; then
    echo "✅ Creating credentials.json from GOOGLE_OAUTH_CREDENTIALS_JSON env var..."
    echo "$GOOGLE_OAUTH_CREDENTIALS_JSON" > credentials.json
    echo "✅ credentials.json created successfully"
else
    echo "ℹ️  GOOGLE_OAUTH_CREDENTIALS_JSON not set (Google Docs export will be disabled)"
fi

# Create token.pickle from environment variable if set (for pre-authenticated tokens)
if [ -n "$GOOGLE_OAUTH_TOKEN_PICKLE_BASE64" ]; then
    echo "✅ Creating token.pickle from GOOGLE_OAUTH_TOKEN_PICKLE_BASE64 env var..."
    echo "$GOOGLE_OAUTH_TOKEN_PICKLE_BASE64" | base64 -d > token.pickle
    echo "✅ token.pickle created successfully"
else
    echo "ℹ️  GOOGLE_OAUTH_TOKEN_PICKLE_BASE64 not set (will need OAuth flow if credentials.json is provided)"
fi

# Check if app.py exists
if [ -f "app.py" ]; then
    echo "✅ app.py found"
else
    echo "❌ ERROR: app.py NOT FOUND"
    ls -la
    exit 1
fi

# Check if gunicorn is available
if command -v gunicorn &> /dev/null; then
    echo "✅ gunicorn found: $(which gunicorn)"
    gunicorn --version
else
    echo "❌ ERROR: gunicorn NOT FOUND"
    exit 1
fi

# Check Playwright browser installation
echo ""
echo "Checking Playwright browsers..."

# Check if Playwright browsers are installed
if python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium; p.stop(); print('Playwright OK')" 2>/dev/null; then
    echo "✅ Playwright browsers available"
else
    echo "⚠️  Playwright browsers may need installation, attempting..."
    playwright install chromium --with-deps 2>&1 || echo "Browser install attempted"
fi

# Also check system chromium as fallback
if command -v chromium &> /dev/null; then
    CHROMIUM_PATH=$(which chromium)
    echo "✅ System Chromium found: $CHROMIUM_PATH"
    if $CHROMIUM_PATH --version &> /dev/null; then
        VERSION=$($CHROMIUM_PATH --version)
        echo "✅ Chromium version: $VERSION"
    fi
else
    echo "ℹ️  System Chromium not in PATH (Playwright will use its own)"
fi

echo ""
echo "========================================="
echo "Starting Gunicorn on 0.0.0.0:${PORT:-8080}"
echo "========================================="

# Start Gunicorn with verbose logging
# NOTE: --preload removed to allow fast health check response
# The app now lazy-loads dependencies on first route access
exec gunicorn app:app \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers 2 \
  --threads 2 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  --log-level debug 2>&1


