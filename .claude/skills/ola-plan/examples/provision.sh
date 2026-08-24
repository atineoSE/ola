#!/usr/bin/env bash
#
# EXAMPLE agent-folder file: <agent-folder>/provision.sh
#
# Installs tooling the sandbox image does not ship. `ola-sandbox`/`ola-monitor`
# run it INSIDE the sandbox on every create and reconnect, as the `agent` user
# (passwordless sudo), before any task starts. A non-zero exit refuses to start
# the sandbox.
#
# This example gives each task worktree its own throwaway PostgreSQL. The
# pattern generalises to any service or toolchain — what to copy is the shape,
# not the package:
#
#   1. Guard on the artifact the install actually produces. `command -v initdb`
#      looks right and is always false, because Ubuntu keeps the server
#      binaries off PATH — the guard never fires and apt re-runs on every
#      reconnect.
#   2. Tolerate a busy apt. sbx runs its own `apt-get update` in the background
#      on every sandbox *start*, so this races it for the lock.
#   3. Expose a stable wrapper on PATH. Tasks read plan text, not install
#      paths; a versioned directory in a task description rots at the next
#      distro bump.
#   4. Give each task a private instance addressed by a per-task PATH, not a
#      shared port. Tasks in one PLAN.md run concurrently; a fixed 5432 turns
#      that into a race.
#
# Requires in allowlist.txt: ports.ubuntu.com, archive.ubuntu.com,
# security.ubuntu.com (apt egress is denied by default).

set -euo pipefail

if ! ls /usr/lib/postgresql/*/bin/initdb >/dev/null 2>&1; then
  APT="sudo apt-get -o DPkg::Lock::Timeout=120"
  $APT update -qq
  DEBIAN_FRONTEND=noninteractive $APT install -y --no-install-recommends postgresql-18
fi

sudo tee /usr/local/bin/ola-pg >/dev/null <<'WRAP'
#!/usr/bin/env bash
# Throwaway PostgreSQL for one worktree, on a unix socket only. No TCP port is
# opened, so N parallel tasks never collide. State lives in <worktree>/.ola/pg,
# which ola git-excludes and deletes with the worktree.
set -euo pipefail
PGROOT="${OLA_PG_DIR:-$PWD/.ola/pg}"
PGDATA="$PGROOT/data"
export PATH="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1):$PATH"
case "${1:-}" in
  start)
    mkdir -p "$PGROOT"
    [ -d "$PGDATA" ] || initdb -D "$PGDATA" -U "$(id -un)" --auth=trust >"$PGROOT/initdb.log" 2>&1
    pg_ctl -D "$PGDATA" -o "-k $PGROOT -c listen_addresses=''" -l "$PGROOT/server.log" -w start >/dev/null
    echo "$PGROOT" ;;
  stop)   pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null 2>&1 || true ;;
  status) pg_ctl -D "$PGDATA" status ;;
  url)    echo "postgresql:///postgres?host=$PGROOT" ;;
  psql)   shift; exec psql -h "$PGROOT" -U "$(id -un)" -d postgres "$@" ;;
  *) echo "usage: ola-pg {start|stop|status|url|psql [args]}" >&2; exit 2 ;;
esac
WRAP
sudo chmod +x /usr/local/bin/ola-pg

echo "provision.sh: ok ($(/usr/lib/postgresql/*/bin/postgres --version))"
