#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy — Replay Prod Snapshots to Dev (with real timestamps)
#
# Interactive script that copies recent JPEG snapshots from the prod S3 bucket
# into the dev S3 bucket and writes matching IMG# records to the dev DynamoDB
# table — preserving the original prod timestamps so dev looks like a real
# historical timelapse.
#
# The script queries prod DynamoDB to list available tenants, sites, and
# cameras, then asks you which one to pull from. It also asks which dev
# site/camera the images should land in.
#
# Prerequisites:
#   - AWS CLI configured with profile "sitespy-dev" (same account for both envs)
#   - jq installed
#   - The dev stack deployed (sam deploy --config-env dev)
#
# Usage:
#   ./scripts/replay-prod-to-dev.sh [--count N] [--days N] [--dry-run]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
PROFILE="sitespy-dev"
REGION="eu-west-2"
PROD_BUCKET="sitespy-prod-snapshots-378202224921"
PROD_TABLE="sitespy-prod-data"
DEV_BUCKET="sitespy-dev-snapshots-378202224921"
DEV_TABLE="sitespy-dev-data"

COUNT=20
DAYS=""
DRY_RUN=false

# ─── Colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
DIM='\033[0;90m'
BOLD='\033[1m'
NC='\033[0m'

# ─── Parse arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --count)    COUNT="$2"; shift 2 ;;
    --days)     DAYS="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=true; shift ;;
    *)          echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  SiteSpy — Replay Prod → Dev (direct copy)${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Choose SOURCE (prod) — tenant, site, camera
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${BOLD}── Source (prod) ──────────────────────────────────────────${NC}"
echo ""

# --- List prod tenants ---
echo -n "Fetching tenants from prod... "
TENANTS_JSON=$(aws dynamodb scan \
  --table-name "$PROD_TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --filter-expression "begins_with(PK, :prefix) AND PK = SK" \
  --expression-attribute-values '{":prefix":{"S":"TENANT#"}}' \
  --projection-expression "PK, tenant_name" \
  --output json 2>/dev/null)

TENANT_IDS=($(echo "$TENANTS_JSON" | jq -r '.Items[].PK.S' | sed 's/TENANT#//' | sort))
TENANT_NAMES=($(echo "$TENANTS_JSON" | jq -r '.Items[].tenant_name.S // "unnamed"' | sort))
echo -e "${GREEN}done${NC}"

if [ ${#TENANT_IDS[@]} -eq 0 ]; then
  echo -e "${RED}No tenants found in prod.${NC}"
  exit 1
fi

echo ""
echo "  Available tenants:"
for i in "${!TENANT_IDS[@]}"; do
  NAME="${TENANT_NAMES[$i]:-}"
  echo -e "    ${CYAN}$((i+1))${NC}) ${TENANT_IDS[$i]}  ${DIM}($NAME)${NC}"
done
echo ""

if [ ${#TENANT_IDS[@]} -eq 1 ]; then
  SOURCE_TENANT="${TENANT_IDS[0]}"
  echo -e "  Only one tenant — auto-selected: ${GREEN}$SOURCE_TENANT${NC}"
else
  read -rp "  Select tenant [1-${#TENANT_IDS[@]}]: " TENANT_CHOICE
  SOURCE_TENANT="${TENANT_IDS[$((TENANT_CHOICE-1))]}"
fi
echo ""

# --- List prod sites for chosen tenant ---
echo -n "Fetching sites for $SOURCE_TENANT... "
SITES_JSON=$(aws dynamodb query \
  --table-name "$PROD_TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
  --expression-attribute-values "{\":pk\":{\"S\":\"TENANT#${SOURCE_TENANT}\"},\":sk\":{\"S\":\"SITE#\"}}" \
  --projection-expression "SK, site_name" \
  --output json 2>/dev/null)

# Filter to site-level items only (exclude camera items which have #CAM# in SK)
SITE_IDS=($(echo "$SITES_JSON" | jq -r '[.Items[] | select(.SK.S | contains("#CAM#") | not) | .SK.S | sub("SITE#";"")] | .[]' | sort))
SITE_NAMES=($(echo "$SITES_JSON" | jq -r '[.Items[] | select(.SK.S | contains("#CAM#") | not) | .site_name.S // "unnamed"] | .[]' | sort))
echo -e "${GREEN}done${NC}"

if [ ${#SITE_IDS[@]} -eq 0 ]; then
  echo -e "${RED}No sites found for tenant $SOURCE_TENANT in prod.${NC}"
  exit 1
fi

echo ""
echo "  Available sites:"
for i in "${!SITE_IDS[@]}"; do
  NAME="${SITE_NAMES[$i]:-}"
  echo -e "    ${CYAN}$((i+1))${NC}) ${SITE_IDS[$i]}  ${DIM}($NAME)${NC}"
done
echo ""

if [ ${#SITE_IDS[@]} -eq 1 ]; then
  SOURCE_SITE="${SITE_IDS[0]}"
  echo -e "  Only one site — auto-selected: ${GREEN}$SOURCE_SITE${NC}"
else
  read -rp "  Select site [1-${#SITE_IDS[@]}]: " SITE_CHOICE
  SOURCE_SITE="${SITE_IDS[$((SITE_CHOICE-1))]}"
fi
echo ""

# --- List prod cameras for chosen site ---
echo -n "Fetching cameras for $SOURCE_SITE... "
CAMERAS_JSON=$(aws dynamodb query \
  --table-name "$PROD_TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
  --expression-attribute-values "{\":pk\":{\"S\":\"TENANT#${SOURCE_TENANT}\"},\":sk\":{\"S\":\"SITE#${SOURCE_SITE}#CAM#\"}}" \
  --projection-expression "SK, camera_name" \
  --output json 2>/dev/null)

CAMERA_IDS=($(echo "$CAMERAS_JSON" | jq -r '.Items[].SK.S' | sed "s/SITE#${SOURCE_SITE}#CAM#//" | sort))
CAMERA_NAMES=($(echo "$CAMERAS_JSON" | jq -r '.Items[].camera_name.S // "unnamed"' | sort))
echo -e "${GREEN}done${NC}"

if [ ${#CAMERA_IDS[@]} -eq 0 ]; then
  echo -e "${RED}No cameras found for site $SOURCE_SITE in prod.${NC}"
  exit 1
fi

echo ""
echo "  Available cameras:"
for i in "${!CAMERA_IDS[@]}"; do
  NAME="${CAMERA_NAMES[$i]:-}"
  echo -e "    ${CYAN}$((i+1))${NC}) ${CAMERA_IDS[$i]}  ${DIM}($NAME)${NC}"
done
echo ""

if [ ${#CAMERA_IDS[@]} -eq 1 ]; then
  SOURCE_CAMERA="${CAMERA_IDS[0]}"
  echo -e "  Only one camera — auto-selected: ${GREEN}$SOURCE_CAMERA${NC}"
else
  read -rp "  Select camera [1-${#CAMERA_IDS[@]}]: " CAM_CHOICE
  SOURCE_CAMERA="${CAMERA_IDS[$((CAM_CHOICE-1))]}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Choose DESTINATION (dev) — site, camera
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Destination (dev) ─────────────────────────────────────${NC}"
echo ""

# --- List dev sites ---
echo -n "Fetching dev sites... "
DEV_SITES_JSON=$(aws dynamodb scan \
  --table-name "$DEV_TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --filter-expression "begins_with(SK, :sk) AND NOT contains(SK, :cam)" \
  --expression-attribute-values '{":sk":{"S":"SITE#"},":cam":{"S":"#CAM#"}}' \
  --projection-expression "PK, SK, site_name" \
  --output json 2>/dev/null)

DEV_TENANT_SITES=($(echo "$DEV_SITES_JSON" | jq -r '.Items[] | "\(.PK.S | sub("TENANT#";""))|\(.SK.S | sub("SITE#";""))|\(.site_name.S // "unnamed")"' | sort))
echo -e "${GREEN}done${NC}"

if [ ${#DEV_TENANT_SITES[@]} -eq 0 ]; then
  echo -e "${RED}No sites found in dev. Run seed-dev.sh first.${NC}"
  exit 1
fi

echo ""
echo "  Available dev sites:"
for i in "${!DEV_TENANT_SITES[@]}"; do
  IFS='|' read -r T S N <<< "${DEV_TENANT_SITES[$i]}"
  echo -e "    ${CYAN}$((i+1))${NC}) ${T} / ${S}  ${DIM}($N)${NC}"
done
echo ""

if [ ${#DEV_TENANT_SITES[@]} -eq 1 ]; then
  IFS='|' read -r DEST_TENANT DEST_SITE DEST_SITE_NAME <<< "${DEV_TENANT_SITES[0]}"
  echo -e "  Only one site — auto-selected: ${GREEN}$DEST_TENANT / $DEST_SITE${NC}"
else
  read -rp "  Select dev site [1-${#DEV_TENANT_SITES[@]}]: " DEV_SITE_CHOICE
  IFS='|' read -r DEST_TENANT DEST_SITE DEST_SITE_NAME <<< "${DEV_TENANT_SITES[$((DEV_SITE_CHOICE-1))]}"
fi
echo ""

# --- List dev cameras for chosen site ---
echo -n "Fetching dev cameras for $DEST_SITE... "
DEV_CAMERAS_JSON=$(aws dynamodb query \
  --table-name "$DEV_TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
  --expression-attribute-values "{\":pk\":{\"S\":\"TENANT#${DEST_TENANT}\"},\":sk\":{\"S\":\"SITE#${DEST_SITE}#CAM#\"}}" \
  --projection-expression "SK, camera_name" \
  --output json 2>/dev/null)

DEV_CAMERA_IDS=($(echo "$DEV_CAMERAS_JSON" | jq -r '.Items[].SK.S' | sed "s/SITE#${DEST_SITE}#CAM#//" | sort))
DEV_CAMERA_NAMES=($(echo "$DEV_CAMERAS_JSON" | jq -r '.Items[].camera_name.S // "unnamed"' | sort))
echo -e "${GREEN}done${NC}"

if [ ${#DEV_CAMERA_IDS[@]} -eq 0 ]; then
  echo -e "${RED}No cameras found for dev site $DEST_SITE.${NC}"
  echo "Run seed-dev.sh or create a camera first."
  exit 1
fi

echo ""
echo "  Available dev cameras:"
for i in "${!DEV_CAMERA_IDS[@]}"; do
  NAME="${DEV_CAMERA_NAMES[$i]:-}"
  echo -e "    ${CYAN}$((i+1))${NC}) ${DEV_CAMERA_IDS[$i]}  ${DIM}($NAME)${NC}"
done
echo ""

if [ ${#DEV_CAMERA_IDS[@]} -eq 1 ]; then
  DEST_CAMERA="${DEV_CAMERA_IDS[0]}"
  echo -e "  Only one camera — auto-selected: ${GREEN}$DEST_CAMERA${NC}"
else
  read -rp "  Select dev camera [1-${#DEV_CAMERA_IDS[@]}]: " DEV_CAM_CHOICE
  DEST_CAMERA="${DEV_CAMERA_IDS[$((DEV_CAM_CHOICE-1))]}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Confirm and copy
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Summary ───────────────────────────────────────────────${NC}"
echo ""
echo "  From:  prod / $SOURCE_TENANT / $SOURCE_SITE / $SOURCE_CAMERA"
echo "  To:    dev  / $DEST_TENANT / $DEST_SITE / $DEST_CAMERA"
echo "  Count: $COUNT most recent snapshots"
if [ -n "$DAYS" ]; then
  echo "  Filter: last $DAYS days only"
fi
if [ "$DRY_RUN" = true ]; then
  echo -e "  Mode:  ${YELLOW}DRY RUN${NC}"
fi
echo ""
read -rp "  Proceed? [Y/n]: " CONFIRM
if [[ "$CONFIRM" =~ ^[Nn] ]]; then
  echo "Cancelled."
  exit 0
fi

echo ""

# ─── List prod snapshots ─────────────────────────────────────────────────────
# Snapshot keys look like:
#   <tenant>/<site>/<camera>/YYYY/MM/DD/<snapshot_ts>.jpg
#
# S3 only ever lists keys in ascending binary order, and that layout sorts
# chronologically — so the NEWEST keys are at the END of the listing. There is
# no "newest first" option in the S3 API.
#
# Do NOT add --max-items here: it truncates from the OLDEST end, so the script
# would pick the newest of the oldest N keys and silently stall weeks behind.
# List the prefix in full (the CLI paginates, ~1 request per 1000 keys) and
# take the tail.
echo -n "Listing snapshots from prod... "

S3_PREFIX="${SOURCE_TENANT}/${SOURCE_SITE}/${SOURCE_CAMERA}/"

ALL_KEYS=$(aws s3api list-objects-v2 \
  --bucket "$PROD_BUCKET" \
  --prefix "$S3_PREFIX" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'Contents[].Key' \
  --output text 2>/dev/null | tr '\t' '\n' | grep '\.jpg$' | sort || true)

# Optional --days cutoff, compared against the capture timestamp in the
# filename. Applied before taking the tail so it bounds how far back we reach
# rather than just trimming the result.
if [ -n "$DAYS" ]; then
  CUTOFF_DATE=$(date -u -v-${DAYS}d +%Y-%m-%d 2>/dev/null \
    || date -u -d "-${DAYS} days" +%Y-%m-%d)
  ALL_KEYS=$(echo "$ALL_KEYS" \
    | awk -F/ -v cutoff="$CUTOFF_DATE" 'substr($NF, 1, 10) >= cutoff')
fi

# Newest $COUNT keys, newest first.
KEYS=$(echo "$ALL_KEYS" | sed '/^$/d' | tail -n "$COUNT" | sort -r)

if [ -z "$KEYS" ]; then
  echo -e "${RED}FAILED${NC}"
  if [ -n "$DAYS" ]; then
    echo "No snapshots in the last $DAYS days under s3://$PROD_BUCKET/$S3_PREFIX"
  else
    echo "No snapshots found under s3://$PROD_BUCKET/$S3_PREFIX"
  fi
  exit 1
fi

KEY_COUNT=$(echo "$KEYS" | wc -l | tr -d ' ')
NEWEST_TS=$(basename "$(echo "$KEYS" | head -1)" .jpg)
OLDEST_TS=$(basename "$(echo "$KEYS" | tail -1)" .jpg)
echo -e "${GREEN}found $KEY_COUNT${NC} ${DIM}($OLDEST_TS → $NEWEST_TS)${NC}"

# ─── Copy snapshots ─────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}Copying snapshots...${NC}"
echo ""

COPIED=0
SKIPPED=0
INDEX=0

while IFS= read -r key; do
  INDEX=$((INDEX + 1))
  FILENAME=$(basename "$key" .jpg)
  SNAPSHOT_TS="$FILENAME"  # e.g. "2026-06-09T14:28:44Z"

  # Build the destination S3 key using the DEV tenant/site/camera and original timestamp
  DATE_PART="${SNAPSHOT_TS:0:10}"  # "2026-06-09"
  YYYY="${DATE_PART:0:4}"
  MM="${DATE_PART:5:2}"
  DD="${DATE_PART:8:2}"
  DEST_KEY="${DEST_TENANT}/${DEST_SITE}/${DEST_CAMERA}/${YYYY}/${MM}/${DD}/${SNAPSHOT_TS}.jpg"

  # Check if already exists in dev (skip if so)
  EXISTS=$(aws s3api head-object \
    --bucket "$DEV_BUCKET" \
    --key "$DEST_KEY" \
    --region "$REGION" \
    --profile "$PROFILE" 2>/dev/null && echo "yes" || echo "no")

  if [ "$EXISTS" = "yes" ]; then
    echo -e "  [$INDEX/$KEY_COUNT] ${DIM}$SNAPSHOT_TS — already in dev, skipping${NC}"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  if [ "$DRY_RUN" = true ]; then
    echo -e "  [$INDEX/$KEY_COUNT] $SNAPSHOT_TS — ${YELLOW}would copy${NC}"
    COPIED=$((COPIED + 1))
    continue
  fi

  echo -n "  [$INDEX/$KEY_COUNT] $SNAPSHOT_TS — "

  # Copy S3 object from prod to dev (server-side copy)
  aws s3 cp "s3://$PROD_BUCKET/$key" "s3://$DEV_BUCKET/$DEST_KEY" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --quiet 2>/dev/null

  # Get object size for the DynamoDB record
  SIZE_BYTES=$(aws s3api head-object \
    --bucket "$DEV_BUCKET" \
    --key "$DEST_KEY" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'ContentLength' \
    --output text 2>/dev/null)

  # Get the SHA-256 from S3 metadata (written at ingest time)
  SHA256=$(aws s3api head-object \
    --bucket "$DEV_BUCKET" \
    --key "$DEST_KEY" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'Metadata.sha256' \
    --output text 2>/dev/null)

  if [ -z "$SHA256" ] || [ "$SHA256" = "None" ]; then
    SHA256="unknown"
  fi

  # Write IMG# record to dev DynamoDB (using dest tenant/site/camera)
  IMG_SK="IMG#${DEST_SITE}#${DEST_CAMERA}#${SNAPSHOT_TS}"
  aws dynamodb put-item \
    --table-name "$DEV_TABLE" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --item "{
      \"PK\": {\"S\": \"TENANT#${DEST_TENANT}\"},
      \"SK\": {\"S\": \"$IMG_SK\"},
      \"s3_key\": {\"S\": \"$DEST_KEY\"},
      \"sha256\": {\"S\": \"$SHA256\"},
      \"size_bytes\": {\"N\": \"$SIZE_BYTES\"},
      \"ingested_at\": {\"S\": \"$SNAPSHOT_TS\"},
      \"content_type\": {\"S\": \"image/jpeg\"}
    }" 2>/dev/null

  echo -e "${GREEN}copied (${SIZE_BYTES} bytes)${NC}"
  COPIED=$((COPIED + 1))

done <<< "$KEYS"

# ─── Summary ────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Done!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Copied:  $COPIED"
echo "  Skipped: $SKIPPED (already in dev)"
echo ""
if [ "$DRY_RUN" = true ]; then
  echo -e "${YELLOW}  This was a dry run — nothing was written.${NC}"
  echo "  Remove --dry-run to execute for real."
fi
