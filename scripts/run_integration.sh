#!/bin/sh
set -eu

repository_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Never inherit a production project name from the caller's environment.
project_name=trades-bks-to-ch-integration

compose() {
    docker compose --env-file "$repository_dir/tests/integration/compose-safe.env" \
        -p "$project_name" -f "$repository_dir/docker-compose.local.yml" "$@"
}

cleanup() {
    exit_code=$?
    trap - EXIT
    if ! compose down --remove-orphans; then
        if [ "$exit_code" -eq 0 ]; then
            exit_code=1
        fi
    fi
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

compose config >/dev/null
compose build ch-init integration container-smoke
compose up -d --wait clickhouse
compose run --rm ch-init
compose run --rm integration
compose run --rm container-smoke
