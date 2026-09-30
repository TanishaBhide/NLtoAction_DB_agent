#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/docs"
OUTPUT_FILE="$OUTPUT_DIR/validation_output.txt"

mkdir -p "$OUTPUT_DIR"

: "This script is a local validation wrapper for a real PostgreSQL installation."
: "It expects psql and a writable DATABASE_URL or PG* environment."

{
  echo "Validation script invoked at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "Root: $ROOT_DIR"
  echo "CSV: $ROOT_DIR/database_raw.csv"
  echo "If psql is available, run the following commands manually or adapt this script to your local PostgreSQL setup."
  echo
  echo "psql -U <user> -d <database> -f database/schema.sql"
  echo "psql -U <user> -d <database> -f database/agent_schema.sql"
  echo "python database/load_data.py --reset"
  echo "psql -U <user> -d <database> -f database/agent_sample_data.sql"
  echo "psql -U <user> -d <database> -f database/queries.sql"
} | tee "$OUTPUT_FILE"