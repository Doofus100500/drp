#!/usr/bin/env bash
set -euo pipefail
umask 077

download_project() {
  local target=$1 url
  local sources=(
    'https://github.com/Doofus100500/drp/archive/refs/heads/master.tar.gz'
    'https://gitlab.com/Doofus100500/drp/-/archive/master/drp-master.tar.gz'
  )
  for url in "${sources[@]}"; do
    echo "Скачивание установщика: $url" >&2
    rm -rf -- "$target/source"
    mkdir -- "$target/source"
    if curl --disable --fail --silent --show-error --location \
      --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 180 \
      --output "$target/project.tar.gz" "$url" \
      && tar -tzf "$target/project.tar.gz" >/dev/null 2>&1 \
      && tar -xzf "$target/project.tar.gz" --strip-components=1 -C "$target/source" \
      && test -f "$target/source/install.sh" \
      && test -f "$target/source/scripts/drp_home.py" \
      && test -f "$target/source/scripts/configure_gitlab.rb" \
      && test -f "$target/source/scripts/verify_repositories.rb" \
      && test -f "$target/source/docs/nginx.md" \
      && test -f "$target/source/templates/infra-git-drp-home-sync.service" \
      && test -f "$target/source/templates/infra-git-drp-home-sync.timer" \
      && test -f "$target/source/templates/infra-git-drp-home-cert.service" \
      && test -f "$target/source/templates/infra-git-drp-home-cert.timer" \
      && bash -n "$target/source/install.sh"; then
      return 0
    fi
    echo "Источник недоступен или архив неполный; пробуем следующий." >&2
  done
  echo "Не удалось скачать проект ни с GitHub, ни с GitLab.com." >&2
  return 1
}

main() (
  if [[ $EUID -ne 0 ]]; then
    echo "Запустите bootstrap.sh через sudo." >&2
    return 1
  fi
  source /etc/os-release
  if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 26.04 || $(uname -m) != x86_64 ]]; then
    echo "Поддерживается Ubuntu 26.04 LTS amd64 (включая 26.04.1)." >&2
    return 1
  fi
  if ! command -v curl >/dev/null || ! command -v tar >/dev/null || ! command -v gzip >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl tar gzip
  fi
  work_dir=$(mktemp -d)
  trap 'rm -rf -- "$work_dir"' EXIT
  download_project "$work_dir"
  # Keep installer errors outside the mirror retry loop. stdin remains the terminal.
  local result=0
  bash "$work_dir/source/install.sh" "$@" || result=$?
  rm -rf -- "$work_dir"
  trap - EXIT
  return "$result"
)

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
