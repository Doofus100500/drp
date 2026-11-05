#!/usr/bin/env bash
set -euo pipefail
umask 077

configure_args=()
if [[ $# -gt 0 ]]; then
  if [[ $# -ne 2 || $1 != --profile ]]; then
    echo "Использование: sudo bash install.sh [--profile /path/to/drp-access.json]" >&2
    exit 1
  fi
  configure_args=(--profile "$2")
fi
if [[ $EUID -ne 0 ]]; then
  echo "Запустите: sudo bash install.sh" >&2
  exit 1
fi
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 26.04 || $(uname -m) != x86_64 ]]; then
  echo "Поддерживается Ubuntu 26.04 LTS amd64 (включая 26.04.1)." >&2
  exit 1
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
echo "Подготовка Ubuntu: устанавливаем зависимости. Затем установщик задаст несколько вопросов."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io restic python3 python3-yaml ca-certificates curl
systemctl enable --now docker
install -d -m 0755 /usr/local/lib/infra-git-drp-home
install -m 0755 "$script_dir/scripts/drp_home.py" /usr/local/lib/infra-git-drp-home/drp_home.py
install -m 0644 "$script_dir/scripts/"*.rb /usr/local/lib/infra-git-drp-home/
install -m 0644 "$script_dir/docs/nginx.md" /usr/local/lib/infra-git-drp-home/nginx.md
ln -sfn /usr/local/lib/infra-git-drp-home/drp_home.py /usr/local/sbin/infra-git-drp-home
for unit in "$script_dir/templates/"*.service "$script_dir/templates/"*.timer; do
  install -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
/usr/local/sbin/infra-git-drp-home configure "${configure_args[@]}"
mode=$(python3 -c 'import json; print(json.load(open("/etc/infra-git-drp-home/config.json"))["mode"])')
if [[ $mode == direct ]]; then
  echo "Настройка HTTPS: устанавливаем nginx и получаем сертификат Let's Encrypt."
  DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot
  systemctl enable --now nginx
  /usr/local/sbin/infra-git-drp-home tls
  systemctl enable --now infra-git-drp-home-cert.timer
fi
echo "Скачиваем и восстанавливаем бэкап. Это может занять продолжительное время."
echo "Следить за ходом в другом терминале: sudo journalctl -fu infra-git-drp-home-sync.service"
systemctl enable --now infra-git-drp-home-sync.timer
systemctl start infra-git-drp-home-sync.service
echo "Восстановление завершено."
/usr/local/sbin/infra-git-drp-home instructions
