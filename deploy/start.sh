#!/bin/sh
# Production start: one Streamlit process; its background worker thread starts with it.
set -e

# Vendor reply links need the app's public address. Render and Railway each publish it
# under their own variable; LEVEL_PUBLIC_URL, if set, wins.
if [ -z "$LEVEL_PUBLIC_URL" ]; then
  if [ -n "$RENDER_EXTERNAL_URL" ]; then
    export LEVEL_PUBLIC_URL="$RENDER_EXTERNAL_URL"
  elif [ -n "$RAILWAY_PUBLIC_DOMAIN" ]; then
    export LEVEL_PUBLIC_URL="https://$RAILWAY_PUBLIC_DOMAIN"
  fi
fi

if [ -z "$LEVEL_PASSCODE" ]; then
  echo "WARNING: LEVEL_PASSCODE is not set; the app is open to anyone with the URL." >&2
fi

mkdir -p "$(dirname "$LEVEL_DB_PATH")" "$LEVEL_UPLOAD_DIR"
python -m level.db init

exec streamlit run app/Home.py --server.address 0.0.0.0 --server.port "${PORT:-8501}"
