#!/usr/bin/python3
"""Install and refresh the home infra-git instance from existing Restic snapshots."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import yaml

BASE = Path("/opt/infra-git-drp-home")
ETC = Path("/etc/infra-git-drp-home")
LIB = Path(__file__).resolve().parent
CONTAINER = "infra-git-drp-home"
NETWORK = "infra-git-drp-home-restore"
LABEL = "infra-git-drp-home.managed"
CERT = "infra-git-drp-home"
DEFAULTS = {"startup_timeout": 1800}
DEFAULT_SSH_PORT = 22222
BACKUP_FIELDS = ("repository", "region", "source_host", "environment")
ACCESS_FIELDS = (*BACKUP_FIELDS, "s3_access_key", "s3_secret_key", "restic_password")


class Failure(Exception):
    """Safe to print. Raw command output is kept only in a root-owned log."""


class BadSnapshot(Failure):
    """Downloaded backup or restored data failed verification; try an older one."""


def say(message):
    print(message, flush=True)


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), mode)
            stream.write(content.encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def save_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


def load_json(path):
    return json.loads(path.read_text())


def read_access_profile(path):
    """Accept the saved wrap payload or the complete Vault unwrap response."""
    try:
        data = load_json(path)
    except (OSError, UnicodeError, ValueError):
        raise Failure("Не удалось прочитать JSON с параметрами доступа.") from None
    if isinstance(data, dict) and "repository" not in data and "data" in data:
        data = data.get("data")
    if not isinstance(data, dict):
        raise Failure("Ожидается JSON-объект из Vault Unwrap.")
    if set(data) == {"password"}:
        raise Failure("В файле только пароль для входа. Для развёртывания нужен полный профиль из группы admins.")
    for key in ACCESS_FIELDS:
        value = data.get(key)
        if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise Failure("В файле доступа отсутствует или некорректно поле: " + key)
    validate_backup_settings(data)
    # Do not install the employee's GitLab password or Vault response metadata.
    return {key: data[key] for key in ACCESS_FIELDS}


def validate_backup_settings(data):
    repository = data.get("repository", "")
    try:
        url = urlsplit(repository[3:])
        valid = (repository.startswith("s3:https://") and url.hostname and url.port != 0
                 and not url.username and not url.password and not url.query and not url.fragment
                 and url.path.strip("/") and not any(c.isspace() for c in repository))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise Failure("repository должен иметь вид s3:https://endpoint/bucket/prefix.")
    for key in ("region", "source_host", "environment"):
        value = data.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise Failure("Некорректное поле конфигурации: " + key)


def domain_name(value):
    value = value.strip().lower().rstrip(".")
    if len(value) > 253 or "." not in value or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        raise Failure("Укажите DNS-имя без протокола, пути и порта.")
    return value


def relative_path(value):
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or any(ord(c) < 32 for c in value)
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise BadSnapshot("Недопустимый путь в бэкапе.")
    return value


def plain_file(root, name):
    path = root / relative_path(name)
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise BadSnapshot("Ссылка вместо ожидаемого файла бэкапа.")
    if not path.is_file():
        raise BadSnapshot("Не хватает обязательного файла бэкапа: " + name)
    return path


def metadata_fields(text):
    # Inspect YAML nodes without constructing Ruby objects (TimeWithZone, Symbol).
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(node, yaml.MappingNode):
            raise ValueError
        fields = {}
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise ValueError
            name = key.value.lstrip(":")
            if name in fields:
                raise ValueError
            if isinstance(value, yaml.ScalarNode):
                fields[name] = value.value
            elif isinstance(value, yaml.SequenceNode):
                fields[name] = [item.value for item in value.value if isinstance(item, yaml.ScalarNode)]
        return fields
    except (ValueError, yaml.YAMLError):
        raise BadSnapshot("Некорректный backup_information.yml.") from None


def inspect_backup(root):
    try:
        metadata = metadata_fields(plain_file(root, "gitlab/backup_information.yml").read_text())
        container = load_json(plain_file(root, "container.json"))
        catalog = load_json(plain_file(root, "repositories.json"))
        secrets = load_json(plain_file(root, "config/gitlab-secrets.json"))
        if not all(isinstance(value, dict) for value in (container, catalog, secrets)):
            raise ValueError
        if not isinstance(secrets.get("gitlab_rails"), dict):
            raise ValueError
        match = re.search(r"(?:^|/)gitlab/gitlab-(ce|ee):(\d+\.\d+\.\d+-(ce|ee)\.\d+)$",
                          container["image"])
        if not match or match[1] != match[3]:
            raise BadSnapshot("В container.json нет точной версии образа GitLab CE/EE.")
        edition, tag = match[1], match[2]
        version = tag.split("-")[0]
        if metadata.get("gitlab_version") not in (version, version + "-" + edition):
            raise BadSnapshot("Версии в container.json и backup_information.yml различаются.")
        if metadata.get("repositories_server_side") == "true":
            raise BadSnapshot("Требуется обычный бэкап репозиториев, не server-side backup.")
        skipped = metadata.get("skipped", "")
        skipped = skipped.split(",") if isinstance(skipped, str) else skipped
        skipped = [item.strip() for item in skipped if item.strip()]
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", item) for item in skipped):
            raise ValueError
        if set(skipped) & {"db", "repositories"}:
            raise BadSnapshot("Бэкап не содержит БД или репозитории.")
        database = plain_file(root, "gitlab/db/database.sql.gz")
        if not database.stat().st_size:
            raise BadSnapshot("Пустой дамп БД.")
        if "registry" not in skipped:
            plain_file(root, "gitlab/registry.tar.gz")
        if catalog["schema_version"] != 1 or not isinstance(catalog["repositories"], list):
            raise ValueError
        storages = {"default"}
        for entry in catalog["repositories"]:
            storage = entry["storage"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", storage) or storage in (".", ".."):
                raise ValueError
            storages.add(storage)
            relative_path(entry["relative_path"])
            if entry["state"] not in ("absent", "empty", "present"):
                raise ValueError
            if entry["state"] != "absent":
                if not entry["steps"]:
                    raise ValueError
                for step in entry["steps"]:
                    plain_file(root / "gitlab/repositories", step["ref_path"])
                    if step.get("bundle_path"):
                        plain_file(root / "gitlab/repositories", step["bundle_path"])
        return {"image": f"docker.io/gitlab/gitlab-{edition}:{tag}", "catalog": catalog,
                "storages": sorted(storages), "skipped": skipped}
    except (KeyError, TypeError, ValueError, IndexError):
        raise BadSnapshot("Некорректные метаданные бэкапа.") from None


def gitlab_config(domain, storages, sidekiq=False, ssh_port=DEFAULT_SSH_PORT):
    storage_entries = ",\n".join(
        "  { name: " + json.dumps(name) + ", path: " +
        json.dumps(f"/var/opt/gitlab/git-data/{name}/repositories") + " }"
        for name in storages
    )
    return f"""# Managed by infra-git-drp-home. Production gitlab.rb is not executed.
external_url 'https://{domain}'
registry_external_url 'https://{domain}:5050'
letsencrypt['enable'] = false
nginx['listen_port'] = 80
nginx['listen_https'] = false
nginx['redirect_http_to_https'] = false
nginx['proxy_set_headers'] = {{
  'Host' => '$http_host', 'X-Forwarded-Proto' => 'https', 'X-Forwarded-Ssl' => 'on'
}}
registry_nginx['listen_port'] = 5000
registry_nginx['listen_https'] = false
registry_nginx['client_max_body_size'] = '0'
registry['registry_http_addr'] = '127.0.0.1:5001'
gitlab_rails['registry_enabled'] = true
gitlab_rails['registry_api_url'] = 'http://127.0.0.1:5001'
gitlab_rails['registry_path'] = '/var/opt/gitlab/gitlab-rails/shared/registry'
gitlab_rails['gitlab_shell_ssh_port'] = {ssh_port}
gitlab_sshd['enable'] = true
gitlab_sshd['listen_address'] = '0.0.0.0:2222'
gitlab_sshd['host_keys_dir'] = '/etc/gitlab/gitlab-sshd'
gitlab_sshd['generate_host_keys'] = true
gitlab_rails['omniauth_enabled'] = false
gitlab_rails['omniauth_auto_sign_in_with_provider'] = nil
gitlab_rails['omniauth_providers'] = []
gitlab_rails['ldap_enabled'] = false
gitlab_rails['smtp_enable'] = false
gitlab_rails['gitlab_email_enabled'] = false
gitlab_rails['incoming_email_enabled'] = false
gitlab_rails['gitlab_default_projects_features_builds'] = false
gitlab_rails['usage_ping_enabled'] = false
gitlab_rails['backup_path'] = '/var/opt/gitlab/backups'
gitlab_rails['backup_keep_time'] = 0
sidekiq['enable'] = {str(sidekiq).lower()}
gitlab_kas['enable'] = false
gitlab_pages['enable'] = false
prometheus_monitoring['enable'] = false
gitaly['configuration'] = {{ storage: [
{storage_entries}
] }}
"""


def nginx_config(domain, backend="127.0.0.1", tls=True, cert_name=CERT):
    content = f"""# Managed by infra-git-drp-home
server {{
    listen 80;
    server_name {domain};
    location ^~ /.well-known/acme-challenge/ {{
        root /var/lib/infra-git-drp-home-acme;
    }}
    location / {{ return 301 https://{domain}$request_uri; }}
}}
"""
    if not tls:
        return content
    for public, upstream in ((443, 8080), (5050, 5080)):
        content += f"""
server {{
    listen {public} ssl;
    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/{cert_name}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{cert_name}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 0;
    location / {{
        proxy_pass http://{backend}:{upstream};
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Port {public};
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $http_connection;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 900s;
    }}
}}
"""
    return content


@contextmanager
def locked(path):
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("Другой запуск уже выполняется.") from None
        yield


class Home:
    def __init__(self, base=BASE, etc=ETC):
        self.base, self.etc = base, etc
        self.config = {**DEFAULTS, **load_json(etc / "config.json")}
        # Existing installations without this field keep their original published port.
        self.config["ssh_port"] = ssh_port_number(self.config.get("ssh_port", 2222))
        validate_backup_settings(self.config)
        self.config["domain"] = domain_name(self.config["domain"])
        if self.config["mode"] not in ("direct", "proxy"):
            raise Failure("Неизвестный режим HTTPS.")
        self.config["bind_ip"] = str(ipaddress.IPv4Address(self.config["bind_ip"]))
        if self.config["mode"] == "direct" and self.config["bind_ip"] != "127.0.0.1":
            raise Failure("В режиме direct backend должен слушать 127.0.0.1.")
        if base.is_symlink() or not (base / ".managed").is_file():
            raise Failure("Каталог данных не принадлежит infra-git-drp-home.")
        self.instance = base / "instance"
        self.staging = base / "staging"
        self.log_path = base / "restore.log"
        self.state_path = base / "state.json"
        self.state = load_json(self.state_path) if self.state_path.exists() else {}

    def run(self, args, label, *, env=None, input=None, timeout=43200, check=True,
            failure=Failure):
        if args[0] == "docker":
            env = dict(os.environ if env is None else env)
            env.pop("DOCKER_CONTEXT", None)
            env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        try:
            result = subprocess.run(args, input=input, text=True, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise Failure(label + ": команда не завершилась.") from None
        with open(self.log_path, "a", opener=lambda p, flags: os.open(p, flags, 0o600)) as log:
            os.chmod(self.log_path, 0o600)
            log.write("\n" + label + "\n" + result.stdout + result.stderr)
        if check and result.returncode:
            if "no space left on device" in (result.stdout + result.stderr).lower():
                raise Failure("Недостаточно места на диске; подробности в restore.log.")
            raise failure(label + ": ошибка; подробности в restore.log.")
        return result

    def restic(self, *args):
        if not args or args[0] not in {"snapshots", "stats", "restore"}:
            raise Failure("Разрешено только чтение Restic-репозитория.")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("RESTIC_", "AWS_"))}
        credentials = load_json(self.etc / "s3.json")
        env.update({key: credentials[key] for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")})
        env.update(RESTIC_REPOSITORY=self.config["repository"],
                   RESTIC_PASSWORD_FILE=str(self.etc / "restic-password"),
                   RESTIC_CACHE_DIR=str(self.base / "cache"),
                   AWS_DEFAULT_REGION=self.config["region"])
        # Read-only S3 credentials work; never create locks or alter the source.
        result = self.run(["restic", "--no-lock", *args], "Restic " + args[0], env=env, check=False)
        if result.returncode:
            detail = (result.stdout + result.stderr).lower()
            corruption = ("ciphertext verification failed", "invalid file content",
                          "invalid file size", "verification failed", "invalid hash")
            if args[0] == "restore" and any(marker in detail for marker in corruption):
                raise BadSnapshot("Restic: проверка целостности snapshot не прошла; см. restore.log.")
            raise Failure("Restic " + args[0] + ": ошибка доступа, сети или диска; см. restore.log.")
        return result.stdout

    def snapshots(self):
        records = json.loads(self.restic("snapshots", "--json", "--host", self.config["source_host"],
                                        "--tag", "gitlab," + self.config["environment"]))
        selected = []
        for item in records:
            if (item.get("hostname") != self.config["source_host"]
                    or not {"gitlab", self.config["environment"]} <= set(item.get("tags", []))):
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", item.get("id", "")):
                raise Failure("Restic вернул некорректный ID snapshot.")
            paths = item.get("paths", [])
            if (len(paths) != 1 or not paths[0].startswith("/")
                    or PurePosixPath(paths[0]).name != ".restic-staging"):
                raise Failure("Неизвестная структура snapshot.")
            relative_path(paths[0][1:])
            datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
            selected.append(item)
        return sorted(selected, key=lambda item: datetime.fromisoformat(
            item["time"].replace("Z", "+00:00")), reverse=True)

    def save_state(self):
        save_json(self.state_path, self.state)

    def remove_tree(self, path):
        if path.parent != self.base or path.name not in ("staging", "instance") or path.is_symlink():
            raise Failure("Отказ от удаления постороннего каталога.")
        if path.exists():
            shutil.rmtree(path)

    def cleanup_applied_backup(self):
        path = self.instance / "data/backups"
        if path.is_symlink() or not path.resolve().is_relative_to(self.instance.resolve()):
            raise Failure("Отказ от удаления постороннего каталога backups.")
        if path.exists():
            shutil.rmtree(path)
        self.remove_tree(self.staging)

    def container(self):
        # Listing succeeds with an empty result when the container does not exist.
        raw = self.run(["docker", "ps", "-aq", "--filter", "name=^/" + CONTAINER + "$"],
                       "Проверка Docker", timeout=30).stdout.strip()
        if not raw:
            return None
        info = json.loads(self.run(["docker", "inspect", CONTAINER], "Проверка контейнера",
                                  timeout=30).stdout)[0]
        if info["Config"].get("Labels", {}).get(LABEL) != "true":
            raise Failure("Контейнер с этим именем не принадлежит проекту.")
        return info

    def remove_container(self):
        if self.container():
            self.run(["docker", "rm", "-f", CONTAINER], "Остановка домашнего GitLab", timeout=120)

    def download(self, snapshot):
        self.remove_tree(self.staging)
        stats = json.loads(self.restic("stats", snapshot["id"], "--mode", "restore-size", "--json"))
        if shutil.disk_usage(self.base).free < int(stats["total_size"]) + 1024**3:
            raise Failure("Недостаточно места для скачивания бэкапа рядом с работающим экземпляром.")
        self.staging.mkdir(mode=0o700)
        say("Скачивание и проверка snapshot " + snapshot["id"])
        self.restic("restore", snapshot["id"] + ":" + snapshot["paths"][0],
                    "--target", str(self.staging), "--verify")
        return inspect_backup(self.staging)

    def start_container(self, image, published):
        args = ["docker", "run", "-d", "--name", CONTAINER, "--hostname", self.config["domain"],
                "--label", LABEL + "=true", "--restart=no", "--shm-size=256m"]
        if published:
            address = self.config["bind_ip"]
            args += ["-p", f"{address}:8080:80", "-p", f"{address}:5080:5000",
                     "-p", f"{self.config['ssh_port']}:2222"]
        else:
            args += ["--network", NETWORK]
        for source, target in ((self.instance / "config", "/etc/gitlab"),
                               (self.instance / "data", "/var/opt/gitlab"),
                               (self.instance / "logs", "/var/log/gitlab"),
                               (self.base / "ssh", "/etc/gitlab/gitlab-sshd")):
            args += ["--mount", f"type=bind,src={source},dst={target}"]
        args += ["--mount", f"type=bind,src={LIB},dst=/drp,readonly", image]
        self.run(args, "Запуск GitLab", timeout=120)

    def wait_ready(self):
        deadline = time.monotonic() + int(self.config["startup_timeout"])
        while time.monotonic() < deadline:
            result = self.run(
                ["docker", "exec", CONTAINER, "/opt/gitlab/embedded/bin/curl",
                 "-fsS", "--max-time", "10", "http://127.0.0.1/-/readiness?all=1"],
                "Ожидание GitLab", timeout=30, check=False)
            if result.returncode == 0:
                return
            time.sleep(10)
        raise BadSnapshot("GitLab не прошёл readiness-проверку за отведённое время.")

    def apply(self, snapshot, backup):
        # Pull and verify prerequisites before discarding the working instance.
        image = self.run(["docker", "image", "inspect", backup["image"]],
                         "Проверка локального образа", timeout=30, check=False)
        if image.returncode:
            self.run(["docker", "pull", backup["image"]], "Подготовка образа GitLab")
        network = self.run(["docker", "network", "ls", "-q", "--filter", "name=^" + NETWORK + "$"],
                           "Проверка сети Docker", timeout=30).stdout.strip()
        if not network:
            self.run(["docker", "network", "create", "--internal", "--label", LABEL + "=true", NETWORK],
                     "Изолированная сеть восстановления", timeout=30)
        else:
            info = json.loads(self.run(["docker", "network", "inspect", NETWORK],
                                      "Проверка сети восстановления", timeout=30).stdout)[0]
            if not info.get("Internal") or info.get("Labels", {}).get(LABEL) != "true":
                raise Failure("Сеть восстановления должна быть внутренней и принадлежать проекту.")
        self.container()  # Refuse collisions before persisting destructive work.
        self.state["previous"] = self.state.get("active") or self.state.get("previous")
        self.state["active"] = None
        self.state["in_progress"] = snapshot["id"]
        self.save_state()
        self.remove_container()
        self.remove_tree(self.instance)
        for name in ("config", "data", "logs"):
            (self.instance / name).mkdir(parents=True, mode=0o700)
        (self.base / "ssh").mkdir(exist_ok=True, mode=0o700)
        shutil.copyfile(plain_file(self.staging, "config/gitlab-secrets.json"),
                        self.instance / "config/gitlab-secrets.json")
        os.chmod(self.instance / "config/gitlab-secrets.json", 0o600)
        atomic_write(self.instance / "config/gitlab.rb",
                     gitlab_config(self.config["domain"], backup["storages"], ssh_port=self.config["ssh_port"]))
        self.start_container(backup["image"], published=False)
        self.wait_ready()
        self.run(["docker", "exec", CONTAINER, "gitlab-ctl", "stop", "puma"], "Остановка Puma")
        backups_dir = self.instance / "data/backups"
        # Fresh GitLab may have generated an empty backups directory.
        if backups_dir.exists():
            if backups_dir.is_symlink() or any(backups_dir.iterdir()):
                raise Failure("Каталог backups нового экземпляра не пуст.")
            backups_dir.rmdir()
        (self.staging / "gitlab").rename(backups_dir)
        self.run(["docker", "exec", CONTAINER, "chown", "-R", "git:git", "/var/opt/gitlab/backups"],
                 "Права на файлы восстановления")
        say("Восстановление GitLab и компонентов snapshot " + snapshot["id"])
        self.run(["docker", "exec", "-e", "GITLAB_ASSUME_YES=1",
                  # tar is packaging, not a data component. Keep refs until verification.
                  "-e", "SKIP=" + ",".join(sorted(set(backup["skipped"]) | {"tar"})),
                  CONTAINER, "gitlab-backup", "restore"],
                 "Восстановление бэкапа", failure=BadSnapshot)
        result = self.run(["docker", "exec", "-i", CONTAINER, "gitlab-rails", "runner",
                           "/drp/configure_gitlab.rb"], "Применение домашних настроек",
                          input=json.dumps(backup["catalog"]), failure=BadSnapshot)
        if "DRP_CONFIGURED " not in result.stdout:
            raise BadSnapshot("GitLab не подтвердил домашние настройки.")
        result = self.run(["docker", "exec", "-i", "-u", "git", CONTAINER,
                           "/opt/gitlab/embedded/bin/ruby", "/drp/verify_repositories.rb"],
                          "Проверка Git-репозиториев и refs", input=json.dumps(backup["catalog"]),
                          failure=BadSnapshot)
        if "DRP_REPOSITORIES_VERIFIED=" not in result.stdout:
            raise BadSnapshot("Не получено подтверждение проверки репозиториев.")
        atomic_write(self.instance / "config/gitlab.rb",
                     gitlab_config(self.config["domain"], backup["storages"], sidekiq=True,
                                   ssh_port=self.config["ssh_port"]))
        self.run(["docker", "exec", CONTAINER, "gitlab-ctl", "reconfigure"],
                 "Применение конфигурации GitLab", failure=BadSnapshot)
        self.run(["docker", "exec", CONTAINER, "gitlab-ctl", "start", "puma"], "Запуск Puma")
        self.wait_ready()
        doctor = self.run(["docker", "exec", CONTAINER, "gitlab-rake", "gitlab:doctor:secrets"],
                          "Проверка ключей шифрования GitLab", failure=BadSnapshot)
        # This Rake task can exit 0 even when encrypted rows cannot be read.
        totals = re.findall(r"Total: (\d+) row\(s\) affected", doctor.stdout + doctor.stderr)
        if totals != ["0"]:
            raise BadSnapshot("Не удалось подтвердить расшифровку данных GitLab.")
        self.verify_registry()
        # Recreate only after successful restoration: no incomplete DB is exposed.
        self.remove_container()
        self.start_container(backup["image"], published=True)
        self.wait_ready()
        self.verify_registry()
        self.run(["docker", "update", "--restart=unless-stopped", CONTAINER], "Автозапуск GitLab")
        self.state.update(active=snapshot["id"], in_progress=None, image=backup["image"],
                          snapshot_time=snapshot["time"], restored_at=datetime.now().astimezone().isoformat())
        self.save_state()
        # Keep no second full local backup. SSH keys, TLS and state survive refreshes.
        self.cleanup_applied_backup()
        say("Успешно восстановлен snapshot " + snapshot["id"])

    def verify_registry(self):
        response = self.run(
            ["docker", "exec", CONTAINER, "/opt/gitlab/embedded/bin/curl", "-sS",
             "--max-time", "30", "-D", "-", "-o", "/dev/null",
             "-H", "Host: " + self.config["domain"] + ":5050", "http://127.0.0.1:5000/v2/"],
            "Проверка Container Registry", timeout=45, failure=BadSnapshot).stdout
        if (not re.search(r"HTTP/\S+ 401\b", response)
                or f'https://{self.config["domain"]}/jwt/auth' not in response
                or "Docker-Distribution-Api-Version: registry/2.0".lower() not in response.lower()):
            raise BadSnapshot("Registry не подтвердил работу API и домашний адрес авторизации.")

    def sync(self):
        with locked(self.base / "sync.lock"):
            self.state = load_json(self.state_path) if self.state_path.exists() else {}
            snapshots = self.snapshots()
            if not snapshots:
                raise Failure("В S3 нет подходящих snapshots.")
            if self.state.get("in_progress"):
                say("Очистка прерванной попытки восстановления.")
                self.remove_container()
                self.remove_tree(self.instance)
            rejected = self.state.setdefault("rejected", {})
            live_ids = {item["id"] for item in snapshots}
            self.state["rejected"] = rejected = {key: value for key, value in rejected.items() if key in live_ids}
            for snapshot in snapshots:
                sid = snapshot["id"]
                if sid in rejected:
                    continue
                if sid == self.state.get("active") and not self.state.get("in_progress"):
                    info = self.container()
                    if not info:
                        raise Failure("Данные отмечены как восстановленные, но контейнер отсутствует.")
                    if not info["State"]["Running"]:
                        self.run(["docker", "start", CONTAINER], "Запуск сохранённого GitLab")
                    # Resume cleanup if power was lost just after saving success.
                    self.cleanup_applied_backup()
                    say("Нового пригодного snapshot нет; работает " + sid)
                    self.save_state()
                    return
                try:
                    backup = self.download(snapshot)
                    self.apply(snapshot, backup)
                    return
                except BadSnapshot as error:
                    say("Snapshot " + sid + ": " + str(error) + " Пробуем предыдущий.")
                    rejected[sid] = str(error)
                    self.save_state()
                    self.remove_tree(self.staging)
                    if self.state.get("in_progress"):
                        self.remove_container()
                        self.remove_tree(self.instance)
                # Infrastructure errors abort here; the timer retries next hour.
            raise Failure("Не осталось пригодных snapshots; проверьте restore.log.")

    def tls(self):
        if self.config["mode"] != "direct":
            raise Failure("В режиме proxy сертификатом управляет существующий nginx.")
        with locked(self.base / "cert.lock"):
            acme = Path("/var/lib/infra-git-drp-home-acme")
            acme.mkdir(exist_ok=True, mode=0o755)
            conf = Path("/etc/nginx/conf.d/infra-git-drp-home.conf")
            if conf.exists() and not conf.read_text().startswith("# Managed by infra-git-drp-home"):
                raise Failure("Файл nginx-конфигурации уже занят.")
            exists = Path(f"/etc/letsencrypt/live/{CERT}/fullchain.pem").is_file()
            atomic_write(conf, nginx_config(self.config["domain"], tls=exists), mode=0o644)
            self.run(["nginx", "-t"], "Проверка nginx", timeout=30)
            self.run(["systemctl", "reload", "nginx"], "Перезагрузка конфигурации nginx", timeout=30)
            self.run(["certbot", "certonly", "--non-interactive", "--agree-tos",
                      "--webroot", "-w", str(acme), "--cert-name", CERT,
                      "-d", self.config["domain"], "--email", self.config["email"]],
                     "Получение сертификата", timeout=900)
            atomic_write(conf, nginx_config(self.config["domain"]), mode=0o644)
            self.run(["nginx", "-t"], "Проверка HTTPS-конфигурации", timeout=30)
            self.run(["systemctl", "reload", "nginx"], "Включение HTTPS", timeout=30)
            hook = Path("/etc/letsencrypt/renewal-hooks/deploy/infra-git-drp-home")
            atomic_write(hook, "#!/bin/sh\nnginx -t && systemctl reload nginx\n", mode=0o755)

    def renew(self):
        if self.config["mode"] != "direct":
            return
        with locked(self.base / "cert.lock"):
            self.run(["certbot", "renew", "--non-interactive", "--no-random-sleep-on-renew",
                      "--cert-name", CERT], "Продление сертификата", timeout=900)


def ask(prompt, parse):
    while True:
        try:
            value = input(prompt)
        except EOFError:
            raise Failure("Ввод завершён. Запустите установщик в терминале и ответьте на вопросы.") from None
        try:
            return parse(value)
        except Failure as error:
            say(str(error) + " Попробуйте ещё раз.")
        except ValueError:
            say("Некорректное значение. Попробуйте ещё раз.")


def https_mode(value):
    modes = {"": "direct", "1": "direct", "direct": "direct", "2": "proxy", "proxy": "proxy"}
    mode = modes.get(value.strip().lower())
    if mode is None:
        raise Failure("Выберите 1 (автоматически) или 2 (свой nginx).")
    return mode


def certificate_email(value):
    value = value.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise Failure("Укажите email, например admin@example.org.")
    return value


def ssh_port_number(value):
    if not re.fullmatch(r"[0-9]+", str(value)) or not 1 <= int(value) <= 65535:
        raise Failure("Укажите номер TCP-порта от 1 до 65535.")
    port = int(value)
    if port in (80, 443, 5050, 8080, 5080):
        raise Failure("Этот порт используется для HTTP/HTTPS GitLab или Registry. Выберите другой.")
    return port


def available_ssh_port(value):
    port = ssh_port_number(value.strip() or DEFAULT_SSH_PORT)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("0.0.0.0", port))
    except OSError:
        raise Failure(f"TCP-порт {port} занят или недоступен на этой Ubuntu. Выберите другой.") from None
    return port


def proxy_guide(domain, address, ssh_port=DEFAULT_SSH_PORT):
    return f"""Настройка своего nginx для {domain}

1. На роутере настройте перенаправление TCP-портов:
   внешние 80, 443, 5050 → сервер nginx, те же номера портов;
   внешний {ssh_port} → Ubuntu с GitLab, порт {ssh_port}.
   Git по SSH подключается напрямую к Ubuntu с GitLab, без HTTP-прокси nginx.
   A-запись {domain} должна указывать на ваш публичный IPv4.
   Не добавляйте AAAA без отдельной настройки IPv6.
   {address} — адрес Ubuntu с GitLab, доступный вашему nginx.
   В nginx укажите HTTP-адрес GitLab: http://{address}:8080; Registry: http://{address}:5080.
   Порты 8080/5080 не пробрасывайте в интернет.

2. Скопируйте с Ubuntu на сервер nginx файлы:
   {ETC}/nginx-http.conf.example
   {ETC}/nginx.conf.example
   Дальнейшие команды выполняются на сервере nginx с обычным nginx и Certbot из apt.
   Если у домена уже есть конфигурация, объедините настройки с ней без второго server block.

3. Для нового сертификата:
   sudo apt-get update && sudo apt-get install -y certbot
   sudo mkdir -p /var/lib/infra-git-drp-home-acme
   Сначала установите nginx-http.conf.example как /etc/nginx/conf.d/infra-git-drp.conf.
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot certonly --webroot -w /var/lib/infra-git-drp-home-acme -d {domain} --cert-name {domain}
   Certbot спросит email и согласие с условиями Let's Encrypt.

4. Установите nginx.conf.example как /etc/nginx/conf.d/infra-git-drp.conf.
   Если используете существующий сертификат, укажите его пути в ssl_certificate и ssl_certificate_key.
   Сохраните Host с портом и X-Forwarded-Proto https в обоих location.
   sudo nginx -t && sudo systemctl reload nginx

5. Для Certbot создайте файл /etc/letsencrypt/renewal-hooks/deploy/reload-nginx:
   Содержимое файла (две строки):
   #!/bin/sh
   nginx -t && systemctl reload nginx
   Затем выполните:
   sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx

6. Для проверки продления после включения сервера выполните sudo systemctl edit certbot.timer:
   [Timer]
   OnCalendar=
   OnCalendar=hourly
   OnBootSec=2min
   Persistent=true
   Затем:
   sudo systemctl daemon-reload
   sudo systemctl enable --now certbot.timer
   sudo certbot renew --dry-run
   Оставьте порт 80 доступным для следующих продлений.

Для Nginx Proxy Manager, контейнерного nginx или Certbot из snap используйте
их средства настройки сертификатов и обновления. Адреса backend указаны в шаге 1.

7. Откройте https://{domain} и проверьте вход своим логином и выданным паролем.
   Затем проверьте clone репозитория и pull известного образа из {domain}:5050.
"""


def access_instructions(config):
    domain = config["domain"]
    if config["mode"] == "proxy":
        guide = proxy_guide(domain, config["bind_ip"], config["ssh_port"])
        atomic_write(ETC / "nginx-setup.txt", guide)
        say("\nОсталось настроить ваш nginx. Памятка сохранена: " + str(ETC / "nginx-setup.txt"))
        say(guide)
    else:
        say("\nОткройте GitLab: https://" + domain)
        say("Войдите своим логином и паролем из поля password в сохранённом JSON.")
        say("Проверьте clone репозитория и pull известного образа.")
    say("Container Registry: " + domain + ":5050")
    say(f"Git по SSH: ssh://git@{domain}:{config['ssh_port']}/<group>/<project>.git")
    say("Бэкапы проверяются каждый час и после включения Ubuntu; локальные изменения заменяются при восстановлении.")
    say("Статус: sudo infra-git-drp-home status")
    say("Журнал: sudo journalctl -u infra-git-drp-home-sync.service")


def configure(profile_path=None):
    ETC.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ETC, 0o700)
    if (ETC / "config.json").exists():
        say("Используется существующая конфигурация " + str(ETC / "config.json"))
        Home(BASE, ETC)
        return
    existing = {item.name for item in BASE.iterdir()} if BASE.exists() else set()
    if BASE.is_symlink() or (existing and existing != {".managed"}):
        raise Failure("Для первой установки нужен пустой каталог " + str(BASE))
    say("\nШаг 1 из 3. Данные доступа")
    say("В сообщении бота откройте Vault → Tools → Unwrap и сохраните весь JSON как drp-access.json.")
    say("Нужен полный профиль admins с настройками бэкапа. Пароль и токен в терминал вставлять не нужно.")
    if profile_path is None:
        say("Если файл в другом каталоге, укажите полный путь, например /home/ubuntu/drp-access.json.")
        profile = ask("Путь к файлу [./drp-access.json]: ",
                      lambda value: read_access_profile(Path(value.strip() or "./drp-access.json").expanduser()))
    else:
        profile = read_access_profile(profile_path)
    say("Файл прочитан; настройки S3 и пароль Restic взяты из него.")
    say("\nШаг 2 из 3. Домен")
    say("Нужен свой домен, например git.example.org. Создайте A-запись на ваш публичный IPv4.")
    say("Не добавляйте AAAA-запись без отдельной настройки IPv6.")
    domain = ask("Домен GitLab (без https:// и порта): ", domain_name)
    say("\nШаг 3 из 3. Сеть и HTTPS")
    say("1 — настроить nginx, сертификат и его продление автоматически на этой Ubuntu.")
    say("2 — использовать свой nginx; после восстановления получите готовые конфиги и памятку.")
    mode = ask("Ваш вариант [1]: ", https_mode)
    say("\nВыберите TCP-порт для git clone/push по SSH, свободный на роутере и на этой Ubuntu.")
    say("Один номер используется снаружи роутера и на Ubuntu с GitLab.")
    ssh_port = ask(f"TCP-порт GitLab для SSH [{DEFAULT_SSH_PORT}]: ", available_ssh_port)
    email = ""
    address = "127.0.0.1"
    if mode == "direct":
        say(f"На роутере перенаправьте TCP 80, 443, 5050 и {ssh_port} на эту Ubuntu, сохранив номера портов.")
        say("Порт 80 понадобится и для выпуска, и для продления сертификата. Порты 8080/5080 оставьте внутренними.")
        say("Перед вводом email завершите настройку DNS и проброс портов: далее начнётся выпуск сертификата.")
        email = ask("Email для регистрации в Let's Encrypt: ", certificate_email)
    else:
        say("\nУкажите локальный IP этой Ubuntu, на которой разворачивается GitLab.")
        say("nginx будет обращаться по этому адресу: порт 8080 — GitLab, порт 5080 — Container Registry.")
        say("Для nginx на другом сервере или в Docker укажите LAN-IP этой Ubuntu, например 192.168.1.10.")
        say("127.0.0.1 подходит, когда nginx установлен прямо на этой Ubuntu и работает вне контейнера.")
        address = ask("IP Ubuntu с GitLab, доступный nginx [127.0.0.1]: ",
                      lambda value: str(ipaddress.IPv4Address(value.strip() or "127.0.0.1")))
        say("На роутере: TCP 80, 443, 5050 → сервер nginx, те же номера портов.")
        say(f"На роутере: TCP {ssh_port} → Ubuntu с GitLab, порт {ssh_port}; SSH идёт напрямую, без nginx.")
        say(f"В nginx: GitLab → http://{address}:8080; Registry → http://{address}:5080.")
        say("Порты 8080 и 5080 доступны nginx внутри вашей сети; проброс этих портов в интернет не нужен.")
    BASE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(BASE, 0o700)
    atomic_write(BASE / ".managed", "infra-git-drp-home\n")
    save_json(ETC / "s3.json", {"AWS_ACCESS_KEY_ID": profile["s3_access_key"],
                                "AWS_SECRET_ACCESS_KEY": profile["s3_secret_key"]})
    atomic_write(ETC / "restic-password", profile["restic_password"] + "\n")
    if mode == "proxy":
        atomic_write(ETC / "nginx-http.conf.example",
                     nginx_config(domain, backend=address, tls=False), mode=0o644)
        atomic_write(ETC / "nginx.conf.example",
                     nginx_config(domain, backend=address, cert_name=domain), mode=0o644)
        atomic_write(ETC / "nginx-setup.txt", proxy_guide(domain, address, ssh_port))
        say("Конфиги и пошаговая памятка nginx созданы в " + str(ETC))
    # Written last, so an incomplete credential setup is not treated as installed.
    save_json(ETC / "config.json", {**DEFAULTS, **{key: profile[key] for key in BACKUP_FIELDS},
                                  "domain": domain, "mode": mode,
                                  "email": email, "bind_ip": address, "ssh_port": ssh_port})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "sync", "status", "tls", "renew", "retry", "instructions"))
    parser.add_argument("--profile", type=Path, help="JSON из Vault Unwrap (только configure)")
    args = parser.parse_args(argv)
    if args.profile is not None and args.command != "configure":
        parser.error("--profile применяется только к configure")
    if os.geteuid() != 0:
        raise Failure("Требуется запуск через sudo.")
    os.umask(0o077)
    if args.command == "configure":
        configure(args.profile)
        return
    home = Home()
    if args.command == "instructions":
        access_instructions(home.config)
    elif args.command == "status":
        say(json.dumps(home.state, indent=2, ensure_ascii=False))
        say("GitLab: https://" + home.config["domain"])
        say("Registry: https://" + home.config["domain"] + ":5050")
        say(f"Git SSH: {home.config['domain']}:{home.config['ssh_port']}")
    elif args.command == "retry":
        with locked(home.base / "sync.lock"):
            home.state = load_json(home.state_path) if home.state_path.exists() else {}
            home.state["rejected"] = {}
            home.save_state()
        home.sync()
    else:
        getattr(home, args.command)()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nОстановлено. Чтобы продолжить, повторите команду установки.")
        sys.exit(130)
    except Failure as error:
        say("ERROR: " + str(error))
        sys.exit(1)
    except Exception:
        # Avoid accidentally publishing S3 credentials or GitLab secrets.
        say("ERROR: непредвиденная ошибка. Проверьте конфигурацию и закрытый restore.log.")
        sys.exit(1)
