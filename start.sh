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
echo "DEBUG: Current working directory is: $(pwd)"
echo "DEBUG: Checking GOOGLE_OAUTH_CREDENTIALS_JSON..."

if [ -n "$GOOGLE_OAUTH_CREDENTIALS_JSON" ]; then
    echo "✅ GOOGLE_OAUTH_CREDENTIALS_JSON is set (length: ${#GOOGLE_OAUTH_CREDENTIALS_JSON})"
    echo "✅ Creating credentials.json using Python (safer for JSON)..."
    # Use Python to write the JSON file - avoids shell escaping issues
    python3 -c "import os; f=open('credentials.json','w'); f.write(os.environ['GOOGLE_OAUTH_CREDENTIALS_JSON']); f.close(); print('Written:', len(os.environ['GOOGLE_OAUTH_CREDENTIALS_JSON']), 'chars')"
    if [ -f "credentials.json" ]; then
        echo "✅ credentials.json created at $(pwd)/credentials.json"
        echo "DEBUG: File size: $(wc -c < credentials.json) bytes"
        echo "DEBUG: First 50 chars: $(head -c 50 credentials.json)"
    else
        echo "❌ Failed to create credentials.json"
    fi
else
    echo "⚠️  GOOGLE_OAUTH_CREDENTIALS_JSON is NOT SET or EMPTY!"
    echo "DEBUG: Check Railway environment variables"
fi

# Create token.pickle from environment variable if set (for pre-authenticated tokens)
echo "DEBUG: GOOGLE_OAUTH_TOKEN_PICKLE_BASE64 length: ${#GOOGLE_OAUTH_TOKEN_PICKLE_BASE64}"

if [ -n "$GOOGLE_OAUTH_TOKEN_PICKLE_BASE64" ]; then
    echo "✅ Creating token.pickle from GOOGLE_OAUTH_TOKEN_PICKLE_BASE64 env var..."
    # Use Python for cross-platform base64 decoding (more reliable than shell base64)
    python3 -c "import base64,os; open('token.pickle','wb').write(base64.b64decode(os.environ['GOOGLE_OAUTH_TOKEN_PICKLE_BASE64']))"
    if [ -f "token.pickle" ]; then
        echo "✅ token.pickle created at $(pwd)/token.pickle"
        echo "DEBUG: File size: $(wc -c < token.pickle) bytes"
    else
        echo "❌ Failed to create token.pickle"
    fi
else
    echo "⚠️  GOOGLE_OAUTH_TOKEN_PICKLE_BASE64 is NOT SET or EMPTY!"
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
echo "FINAL FILE CHECK before starting Gunicorn"
echo "========================================="
echo "Current directory: $(pwd)"
echo "Listing credential files:"
ls -la credentials.json token.pickle 2>&1 || echo "Some files missing"
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


