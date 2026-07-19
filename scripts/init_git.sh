#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -d .git ]]; then git init; fi
git add .
printf 'Git repository initialized and files staged. Review with: git status\n'
