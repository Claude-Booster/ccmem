#!/usr/bin/env bash
# Self-test for the commit-hook suite. Proves the hooks actually BLOCK, so a
# future edit that silently stops matching (e.g. a broken regex) is caught here
# instead of in production. Runs against a throwaway repo in a temp dir; never
# touches the real .blocked. Exits non-zero if any assertion fails.
#
#   bash .githooks/test-hooks.sh
set -u

SRC="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); printf '  PASS: %s\n' "$1"; }
no(){ FAIL=$((FAIL+1)); printf '  FAIL: %s\n' "$1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.githooks"
for h in pre-commit commit-msg pre-push tag; do cp "$SRC/$h" "$TMP/.githooks/"; done
# Test patterns — deliberately NOT the real identifiers.
printf 'ZZBLOCKNAMEZZ\nzzblock\\.user@example\\.com\n' > "$TMP/.githooks/.blocked"

cd "$TMP" || exit 1
git init -q
git config core.hooksPath .githooks
git config user.name  "Developer"
git config user.email "dev@users.noreply.github.com"
git config commit.gpgsign false

unstage(){ git rm --cached -f "$1" >/dev/null 2>&1; rm -f "$1"; }
precommit(){ sh .githooks/pre-commit >/dev/null 2>&1; }

# 1. content leak -> blocked
printf 'hello ZZBLOCKNAMEZZ world\n' > c.txt; git add c.txt
precommit && no "content leak blocked" || ok "content leak blocked"; unstage c.txt

# 2. clean content -> passes
printf 'nothing sensitive here\n' > ok.txt; git add ok.txt
precommit && ok "clean content passes" || no "clean content passes"; unstage ok.txt

# 3. filename leak -> blocked (clean content, offending path)
printf 'clean\n' > "ZZBLOCKNAMEZZ-notes.txt"; git add "ZZBLOCKNAMEZZ-notes.txt"
precommit && no "filename leak blocked" || ok "filename leak blocked"; unstage "ZZBLOCKNAMEZZ-notes.txt"

# 4. committer identity leak -> blocked
git config user.name "ZZBLOCKNAMEZZ"
printf 'clean\n' > i.txt; git add i.txt
precommit && no "identity leak blocked" || ok "identity leak blocked"
git config user.name "Developer"; unstage i.txt

# 5. commit message leak -> blocked
printf 'fix: reported by ZZBLOCKNAMEZZ\n' > m.txt
sh .githooks/commit-msg m.txt >/dev/null 2>&1 && no "commit-msg leak blocked" || ok "commit-msg leak blocked"
rm -f m.txt

# 6. invalid ERE in .blocked -> guard skips loudly (exit 0), never a silent crash/block
printf '[unclosed(char\n' > .githooks/.blocked
printf 'clean\n' > g.txt; git add g.txt
precommit && ok "invalid-pattern guard stays fail-safe (exit 0)" || no "invalid-pattern guard stays fail-safe"; unstage g.txt

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
