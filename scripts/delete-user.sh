#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy — Interactive User Deletion
#
# Lists users from Cognito, lets you pick one, and removes them from both
# Cognito and DynamoDB.
#
# Usage:
#   ./scripts/delete-user.sh          # defaults to prod
#   ./scripts/delete-user.sh --env dev
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ─── Parse args ──────────────────────────────────────────────────────────────
ENV="prod"
while [[ $# -gt 0 ]]; do
  case $1 in
    --env) ENV="$2"; shift 2 ;;
    *)     echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ─── Config per environment ──────────────────────────────────────────────────
if [ "$ENV" = "prod" ]; then
  PROFILE="sitespy-dev"
  USER_POOL_ID="eu-west-2_XXwnO0sjc"
  TABLE="sitespy-prod-data"
elif [ "$ENV" = "dev" ]; then
  PROFILE="sitespy-dev"
  USER_POOL_ID="eu-west-2_hSZjNVtPO"
  TABLE="sitespy-dev-data"
else
  echo "Error: --env must be 'dev' or 'prod'"
  exit 1
fi

REGION="eu-west-2"

# ─── Colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── Banner ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  SiteSpy — Delete User ${DIM}($ENV)${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"

# ─── Fetch users from Cognito ────────────────────────────────────────────────
echo ""
echo -e "${DIM}  Fetching users from Cognito...${NC}"

USER_JSON=$(aws cognito-idp list-users \
  --user-pool-id "$USER_POOL_ID" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --output json 2>/dev/null)

# Parse into arrays
USERNAMES=()
EMAILS=()
SUBS=()
STATUSES=()
LABELS=()

while IFS='|' read -r username email sub status; do
  USERNAMES+=("$username")
  EMAILS+=("$email")
  SUBS+=("$sub")
  STATUSES+=("$status")
  LABELS+=("$email  ${DIM}($status, sub: ${sub:0:8}...)${NC}")
done < <(echo "$USER_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for user in data.get('Users', []):
    username = user['Username']
    status = user.get('UserStatus', 'UNKNOWN')
    attrs = {a['Name']: a['Value'] for a in user.get('Attributes', [])}
    email = attrs.get('email', username)
    sub = attrs.get('sub', username)
    print(f'{username}|{email}|{sub}|{status}')
" 2>/dev/null)

if [ ${#USERNAMES[@]} -eq 0 ]; then
  echo -e "  ${RED}No users found in the user pool.${NC}"
  exit 0
fi

# ─── Display user list ───────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}  Select user to delete:${NC}"
echo -e "${DIM}  ─────────────────────────────────────────${NC}"
for i in "${!LABELS[@]}"; do
  printf "  ${BOLD}%d)${NC} %b\n" $((i + 1)) "${LABELS[$i]}"
done
echo ""

COUNT=${#USERNAMES[@]}
while true; do
  read -rp "  Choose [1-$COUNT]: " selection
  if [[ "$selection" =~ ^[0-9]+$ ]] && [ "$selection" -ge 1 ] && [ "$selection" -le "$COUNT" ]; then
    IDX=$((selection - 1))
    break
  fi
  echo -e "  ${RED}Invalid choice.${NC}"
done

SELECTED_USERNAME="${USERNAMES[$IDX]}"
SELECTED_EMAIL="${EMAILS[$IDX]}"
SELECTED_SUB="${SUBS[$IDX]}"

# ─── Confirmation ────────────────────────────────────────────────────────────
echo ""
echo -e "${RED}  ⚠  You are about to DELETE:${NC}"
echo -e "     Email: ${BOLD}$SELECTED_EMAIL${NC}"
echo -e "     Sub:   $SELECTED_SUB"
echo ""
echo -e "  This will remove them from Cognito and DynamoDB. ${BOLD}This cannot be undone.${NC}"
echo ""
read -rp "$(echo -e "  ${RED}Type the email to confirm:${NC} ")" CONFIRM_EMAIL

if [ "$CONFIRM_EMAIL" != "$SELECTED_EMAIL" ]; then
  echo -e "  ${DIM}Email didn't match. Cancelled.${NC}"
  exit 0
fi

# ─── Delete from Cognito ─────────────────────────────────────────────────────
echo ""
echo -n "  Deleting from Cognito... "
aws cognito-idp admin-delete-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$SELECTED_USERNAME" \
  --region "$REGION" \
  --profile "$PROFILE" 2>/dev/null
echo -e "${GREEN}done${NC}"

# ─── Delete User_Record from DynamoDB ────────────────────────────────────────
# Scan for the USER# record matching this sub OR email (Cognito sometimes uses
# email as the username/sub depending on how the user was created)
echo -n "  Finding User_Record in DynamoDB... "

DYNAMO_RECORDS=$(aws dynamodb scan \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --filter-expression "begins_with(SK, :sk1) OR begins_with(SK, :sk2) OR email = :email" \
  --expression-attribute-values "{\":sk1\":{\"S\":\"USER#$SELECTED_SUB\"}, \":sk2\":{\"S\":\"USER#$SELECTED_EMAIL\"}, \":email\":{\"S\":\"$SELECTED_EMAIL\"}}" \
  --projection-expression "PK, SK" \
  --output json 2>/dev/null)

RECORD_COUNT=$(echo "$DYNAMO_RECORDS" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('Items',[])))" 2>/dev/null)

if [ "$RECORD_COUNT" -gt 0 ]; then
  echo -e "${GREEN}found $RECORD_COUNT record(s)${NC}"

  # Delete each matching record
  echo "$DYNAMO_RECORDS" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('Items', []):
    pk = item['PK']['S']
    sk = item['SK']['S']
    print(f'{pk}|{sk}')
" 2>/dev/null | while IFS='|' read -r pk sk; do
    echo -n "  Deleting $pk / $sk... "
    aws dynamodb delete-item \
      --table-name "$TABLE" \
      --region "$REGION" \
      --profile "$PROFILE" \
      --key "{\"PK\":{\"S\":\"$pk\"}, \"SK\":{\"S\":\"$sk\"}}" 2>/dev/null
    echo -e "${GREEN}done${NC}"
  done
else
  echo -e "${DIM}no DynamoDB record found (may not have been created)${NC}"
fi

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ User deleted: $SELECTED_EMAIL${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
