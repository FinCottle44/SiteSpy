#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy — Nuke Dev Environment
#
# Wipes ALL data from the dev environment:
#   - All items in DynamoDB (sitespy-dev-data)
#   - All objects in S3 (sitespy-dev-snapshots-*)
#   - All Cognito users (except the ones you re-create via seed)
#
# This is DESTRUCTIVE and IRREVERSIBLE. Dev only.
#
# Usage:
#   ./scripts/nuke-dev.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="sitespy-dev"
REGION="eu-west-2"
TABLE="sitespy-dev-data"
USER_POOL_ID="eu-west-2_hSZjNVtPO"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${RED}═══════════════════════════════════════════════════════════${NC}"
echo -e "${RED}  ⚠  NUKE DEV ENVIRONMENT  ⚠${NC}"
echo -e "${RED}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "This will DELETE:"
echo "  • All DynamoDB items in $TABLE"
echo "  • All S3 objects in the snapshots bucket"
echo "  • All Cognito users in $USER_POOL_ID"
echo ""
read -p "Type 'nuke' to confirm: " CONFIRM

if [ "$CONFIRM" != "nuke" ]; then
  echo "Aborted."
  exit 1
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Find and empty the S3 bucket
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/3] Emptying S3 snapshots bucket...${NC}"

BUCKET=$(aws cloudformation describe-stacks \
  --stack-name sitespy-dev \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'Stacks[0].Outputs[?OutputKey==`SnapshotsBucketName`].OutputValue' \
  --output text 2>/dev/null)

if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
  # Delete all object versions (bucket has versioning enabled)
  echo "  Bucket: $BUCKET"
  echo "  Deleting all object versions..."
  aws s3api list-object-versions \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
    --output json 2>/dev/null | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
objects = data.get('Objects') or []
if not objects:
    print('  No objects to delete.')
    sys.exit(0)
# Batch delete in groups of 1000
for i in range(0, len(objects), 1000):
    batch = objects[i:i+1000]
    delete_payload = json.dumps({'Objects': batch, 'Quiet': True})
    print(f'  Deleting batch of {len(batch)} objects...')
    sys.stdout.flush()
    import subprocess
    subprocess.run([
        'aws', 's3api', 'delete-objects',
        '--bucket', '$BUCKET',
        '--region', '$REGION',
        '--profile', '$PROFILE',
        '--delete', delete_payload
    ], capture_output=True)
print(f'  Deleted {len(objects)} object versions total.')
"

  # Also delete any delete markers
  aws s3api list-object-versions \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
    --output json 2>/dev/null | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
objects = data.get('Objects') or []
if not objects:
    sys.exit(0)
import subprocess
for i in range(0, len(objects), 1000):
    batch = objects[i:i+1000]
    delete_payload = json.dumps({'Objects': batch, 'Quiet': True})
    subprocess.run([
        'aws', 's3api', 'delete-objects',
        '--bucket', '$BUCKET',
        '--region', '$REGION',
        '--profile', '$PROFILE',
        '--delete', delete_payload
    ], capture_output=True)
print(f'  Cleaned up {len(objects)} delete markers.')
"
  echo -e "  ${GREEN}S3 bucket emptied.${NC}"
else
  echo -e "  ${YELLOW}Could not find snapshots bucket. Skipping.${NC}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. Delete all DynamoDB items
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[2/3] Deleting all DynamoDB items...${NC}"

python3 -c "
import boto3, sys

session = boto3.Session(profile_name='$PROFILE', region_name='$REGION')
dynamodb = session.client('dynamodb')
table = '$TABLE'

count = 0
scan_kwargs = {'TableName': table, 'ProjectionExpression': 'PK, SK'}

while True:
    response = dynamodb.scan(**scan_kwargs)
    items = response.get('Items', [])
    
    for item in items:
        dynamodb.delete_item(TableName=table, Key={'PK': item['PK'], 'SK': item['SK']})
        count += 1
    
    last_key = response.get('LastEvaluatedKey')
    if not last_key:
        break
    scan_kwargs['ExclusiveStartKey'] = last_key

print(f'  Deleted {count} items from DynamoDB.')
"
echo -e "  ${GREEN}DynamoDB table emptied.${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Delete all Cognito users
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[3/3] Deleting all Cognito users...${NC}"

USERS=$(aws cognito-idp list-users \
  --user-pool-id "$USER_POOL_ID" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'Users[].Username' \
  --output text 2>/dev/null)

if [ -n "$USERS" ] && [ "$USERS" != "None" ]; then
  for USER in $USERS; do
    aws cognito-idp admin-delete-user \
      --user-pool-id "$USER_POOL_ID" \
      --username "$USER" \
      --region "$REGION" \
      --profile "$PROFILE" 2>/dev/null
    echo "  Deleted user: $USER"
  done
  echo -e "  ${GREEN}All Cognito users deleted.${NC}"
else
  echo "  No users to delete."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Dev environment is clean.${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/seed-dev.sh to re-seed with fresh data"
echo "  2. Re-create your Cognito users (the seed script will guide you)"
