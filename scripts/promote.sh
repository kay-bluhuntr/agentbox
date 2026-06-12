#!/usr/bin/env bash
# Promote the image currently deployed in dev to production.
#
# Promotion is deliberately boring: copy dev's pinned tag into prod's values
# file and open a pull request. The PR diff is the deployment record; ArgoCD
# applies it on merge. No kubectl, no helm, no SSH to anything.
set -euo pipefail

DEV_VALUES="deploy/envs/dev/values.yaml"
PROD_VALUES="deploy/envs/prod/values.yaml"

tag=$(grep -E '^\s+tag:' "$DEV_VALUES" | head -1 | sed -E 's/.*tag: *"?([^"]*)"?.*/\1/')
if [[ -z "$tag" || "$tag" == "initial" ]]; then
  echo "error: no built image tag found in $DEV_VALUES — has CI run on main yet?" >&2
  exit 1
fi

current=$(grep -E '^\s+tag:' "$PROD_VALUES" | head -1 | sed -E 's/.*tag: *"?([^"]*)"?.*/\1/')
if [[ "$current" == "$tag" ]]; then
  echo "prod is already on $tag — nothing to promote"
  exit 0
fi

branch="promote/${tag:0:7}"
git checkout -b "$branch"
sed -i.bak -E "s|^(\s+tag:).*|\1 \"$tag\"  # promoted from dev via PR — see scripts/promote.sh|" "$PROD_VALUES"
rm -f "${PROD_VALUES}.bak"
git add "$PROD_VALUES"
git commit -m "promote: ${tag:0:7} to production"

echo
echo "Created branch $branch promoting ${current:0:7} -> ${tag:0:7}."
echo "Push and open a PR:"
echo
echo "  git push -u origin $branch"
echo "  gh pr create --title 'Promote ${tag:0:7} to production' \\"
echo "    --body 'Image verified in dev. ArgoCD will sync prod on merge.'"
