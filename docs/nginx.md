# Свой nginx: пошаговая настройка

Инструкция для отдельного сервера с nginx и Certbot из apt. GitLab уже восстановлен
на Ubuntu, при установке выбран вариант **2 — свой nginx**.

В примере используются:

| Параметр | Значение |
| --- | --- |
| Домен GitLab | `git.example.org` |
| IP Ubuntu с GitLab, доступный nginx | `192.168.1.10` |
| Внешний SSH-порт GitLab | `22222` |

Замените эти значения своими. В `/etc/infra-git-drp-home/nginx-setup.txt`
они уже подставлены установщиком. `127.0.0.1` подходит только для nginx,
работающего непосредственно на той же Ubuntu, вне контейнера.

Для Nginx Proxy Manager, nginx в Docker или Certbot из snap используйте их способ
подключения конфигурации и управления сертификатами. Схема портов и проверки ниже
остаются такими же; команды apt и systemctl относятся к обычной установке на сервере.

## 1. Настройте DNS, роутер и firewall

Создайте A-запись `git.example.org` на публичный IPv4 роутера.
AAAA добавляйте только при настроенной маршрутизации и firewall для IPv6.

На роутере настройте TCP:

| Публичный порт | Куда направить |
| --- | --- |
| `80`, `443`, `5050` | Сервер nginx, те же порты |
| `22222` | Ubuntu с GitLab, порт `22222` |

SSH подключается напрямую к Ubuntu с GitLab. Порты `8080` и `5080` нужны
только для связи nginx с GitLab внутри сети; не создавайте для них проброс в интернет.

Проверьте firewall на обеих машинах:

1. На nginx разрешены входящие TCP `80/443/5050` и исходящие соединения
   к `192.168.1.10:8080` и `192.168.1.10:5080`.
2. На Ubuntu разрешены соединения от nginx к `8080/5080` и входящий TCP `22222`.
3. Внешний доступ к `8080/5080` закрыт на роутере и в правилах для Docker.

Docker публикует порты своими правилами; одного `ufw deny` может быть недостаточно.
Используйте правила для выбранного firewall backend Docker и проверьте доступ
фактическими соединениями в шагах 2 и 8. [Docker и firewall](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

## 2. Проверьте соединение nginx → GitLab

**На сервере nginx:**

```bash
curl --connect-timeout 5 --max-time 15 -sS -o /dev/null -w '%{http_code}\n' -H 'Host: git.example.org' http://192.168.1.10:8080/users/sign_in
curl --connect-timeout 5 --max-time 15 -sS -D - -o /dev/null -H 'Host: git.example.org:5050' http://192.168.1.10:5080/v2/
```

Ожидается `200` или перенаправление `30x` от GitLab. Для Registry нормален `401`
с `Docker-Distribution-Api-Version: registry/2.0` и `WWW-Authenticate`, содержащим
`realm="https://git.example.org/jwt/auth"`. Тайм-аут или отказ в соединении нужно
устранить до настройки HTTPS; `502/503` требуют проверки готовности GitLab.

**На Ubuntu с GitLab:**

```bash
sudo docker port infra-git-drp-home
ssh-keyscan -T 5 -p 22222 192.168.1.10
```

В выводе Docker должны быть привязки `8080 → 80`, `5080 → 5000`,
`22222 → 2222` контейнера; `ssh-keyscan` должен получить SSH host key.
Если ключ не получен, проверьте публикацию SSH-порта и firewall Ubuntu.
Если этих привязок ещё нет, дождитесь успешного завершения восстановления.

## 3. Подготовьте файлы и сохраните текущую конфигурацию

Скопируйте с Ubuntu в текущий каталог на сервере nginx:

```text
/etc/infra-git-drp-home/nginx-http.conf.example
/etc/infra-git-drp-home/nginx.conf.example
```

Далее команды выполняются **на сервере nginx**, из каталога с этими файлами.
Сохраните резервную копию всей конфигурации, включая существующие vhost:

```bash
sudo cp -a /etc/nginx "/etc/nginx.before-drp-$(date +%Y%m%d-%H%M%S)"
sudo nginx -t
```

Найдите существующие настройки `server_name git.example.org` в выводе
`sudo nginx -T`. Не создавайте второй блок с той же парой `listen` и `server_name`:
объедините настройки соответствующего порта с существующим vhost.
Отдельные блоки для `80`, `443` и `5050` — это нормально.

Команды `install` ниже подходят для нового vhost. Если vhost уже есть,
перенесите настройки в его файл вручную вместо создания дополнительного файла.
Убедитесь, что `/etc/nginx/conf.d/*.conf` подключается внутри контекста `http`.

## 4. Подготовьте HTTP-проверку Let's Encrypt

Если vhost уже есть, добавьте в его блок TCP 80 только `location` для
`/.well-known/acme-challenge/` из HTTP-примера, сохранив действующие TLS-блоки.
В таком случае пропустите команду `install` ниже.

```bash
sudo apt-get update
sudo apt-get install -y certbot
sudo mkdir -p /var/lib/infra-git-drp-home-acme/.well-known/acme-challenge
sudo install -m 0644 nginx-http.conf.example /etc/nginx/conf.d/infra-git-drp.conf
sudo nginx -t && sudo systemctl reload nginx
printf 'drp-acme-ok\n' | sudo tee /var/lib/infra-git-drp-home-acme/.well-known/acme-challenge/drp-check >/dev/null
```

**Из внешней сети**, например через мобильный интернет, выполните:

```bash
curl --fail --connect-timeout 5 --max-time 15 http://git.example.org/.well-known/acme-challenge/drp-check
```

Ожидается `drp-acme-ok`. Это проверяет DNS, проброс TCP 80 и нужный HTTP vhost.
Затем на сервере nginx удалите тестовый файл:

```bash
sudo rm /var/lib/infra-git-drp-home-acme/.well-known/acme-challenge/drp-check
```

## 5. Получите сертификат

Если подходящий сертификат уже есть, пропустите команду выпуска и укажите его пути
в обоих TLS-блоках файла `nginx.conf.example`. Для сертификата, обслуживаемого другим
ACME-клиентом, сохраните его действующий механизм продления вместо шага 7.

**На сервере nginx:**

```bash
sudo certbot certonly --webroot -w /var/lib/infra-git-drp-home-acme -d git.example.org --cert-name git.example.org
```

Ответьте на вопросы Certbot. Ожидаются файлы `fullchain.pem` и `privkey.pem`
в `/etc/letsencrypt/live/git.example.org/`.

## 6. Включите HTTPS для GitLab и Registry

Проверьте `server_name`, адреса `proxy_pass` и пути сертификата в `nginx.conf.example`.
В начале файла находится `map $http_upgrade $drp_connection_upgrade`: он должен
быть в контексте `http`, вне `server` и `location`, и объявляться один раз.
Оба `location` используют `$drp_connection_upgrade` вместо заголовка клиента.
[Шаблон WebSocket от nginx](https://nginx.org/en/docs/http/websocket.html).

`Host $http_host` выбран намеренно: он сохраняет порт `:5050` у Registry.
Сохраните также `X-Forwarded-Proto https` в обоих `location`.

```bash
sudo install -m 0644 nginx.conf.example /etc/nginx/conf.d/infra-git-drp.conf
sudo nginx -t && sudo systemctl reload nginx
```

Не переходите дальше, если `nginx -t` завершился ошибкой. Восстановите изменённый
vhost из копии шага 3 или исправьте указанную nginx строку, затем повторите проверку.

## 7. Настройте и проверьте продление сертификата

**На сервере nginx:** создайте deploy hook, чтобы nginx перечитал сертификат после
успешного продления. Имя файла ниже выделено для DRP; существующие hooks сохраняйте.

```bash
sudo install -d /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/infra-git-drp-nginx >/dev/null <<'SH'
#!/bin/sh
set -e
nginx -t
systemctl reload nginx
SH
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/infra-git-drp-nginx
sudo /etc/letsencrypt/renewal-hooks/deploy/infra-git-drp-nginx
```

Почасовая проверка нужна здесь для DRP: после включения долго выключенного сервера
Certbot быстро проверит необходимость продления. Для постоянно работающего nginx
обычно достаточно штатного `certbot.timer`; менять его расписание необязательно.
Override ниже изменяет общий таймер Certbot на этом сервере.

```bash
sudo install -d /etc/systemd/system/certbot.timer.d
sudo tee /etc/systemd/system/certbot.timer.d/infra-git-drp.conf >/dev/null <<'TIMER'
[Timer]
OnCalendar=
OnCalendar=hourly
OnBootSec=2min
Persistent=true
RandomizedDelaySec=1min
TIMER
sudo systemctl daemon-reload
sudo systemctl enable --now certbot.timer
sudo systemctl list-timers certbot.timer
sudo certbot renew --cert-name git.example.org --dry-run --run-deploy-hooks
```

Ожидается успешное тестовое продление и выполнение deploy hook. Обычный
`renew --dry-run` не запускает deploy hooks; флаг `--run-deploy-hooks` включает их
с действующим сертификатом. При другом имени сертификата замените `--cert-name`.
Для проверки журналов используйте `sudo journalctl -u certbot.service -n 50 --no-pager`
и `/var/log/letsencrypt/letsencrypt.log`. Оставьте TCP 80 доступным для продлений.
[Документация Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates).

## 8. Проверьте публичные адреса и SSH

Выполните проверки **из внешней сети**, а не только с сервера nginx. Не добавляйте
`-k` к curl: ошибки доверия к сертификату или несовпадение домена нужно устранить.

```bash
curl --fail --connect-timeout 5 --max-time 15 -sS -o /dev/null -w '%{http_code}\n' https://git.example.org/users/sign_in
curl --connect-timeout 5 --max-time 15 -sS -D - -o /dev/null https://git.example.org:5050/v2/
ssh-keyscan -T 5 -p 22222 git.example.org
curl --connect-timeout 5 --max-time 10 -I http://git.example.org:8080
curl --connect-timeout 5 --max-time 10 -I http://git.example.org:5080
```

Первые две команды проверяют TLS отдельно на `443` и `5050`. GitLab отвечает `200`
или `30x`; Registry — `401` с HTTPS realm `https://git.example.org/jwt/auth` и
`Docker-Distribution-Api-Version: registry/2.0`. Этот `401` ожидается без авторизации.

SSH host key должен совпасть с полученным на Ubuntu в шаге 2. Последние две команды
должны завершиться отказом в соединении или тайм-аутом: backend-порты закрыты извне.
Если у DNS несколько A/AAAA, проверьте каждый опубликованный адрес.

Откройте `https://git.example.org` и войдите выданным локальным паролем.
Проверьте clone своего репозитория; путь `group/project` замените существующим:

```bash
git clone ssh://git@git.example.org:22222/group/project.git
```

Для SSH нужен ключ, добавленный в вашу учётную запись GitLab.

## 9. Проверьте Registry: login, push и pull

**На клиенте с Docker во внешней сети:** используйте свой логин и пароль.
При включённой 2FA нужен токен домашнего GitLab со scopes `read_registry` и
`write_registry`. Пароль или токен вводите в запрос Docker, а не в командную строку.
[Авторизация Registry](https://docs.gitlab.com/user/packages/container_registry/authenticate_with_container_registry/).

```bash
docker login git.example.org:5050
```

Сначала скачайте один известный образ из восстановленного Registry. Его полный
путь возьмите на странице проекта **Deploy → Container Registry**, заменив старый
адрес Registry на `git.example.org:5050`.

Для полной приёмки проверьте загрузку нового слоя и его скачивание. Возьмите
отдельный тестовый проект, в который у вас есть право push; замените
`group/test-project` ниже. Эта проверка создаёт тестовый образ в домашнем GitLab.
Локальные изменения исчезнут при следующем восстановлении бэкапа.

```bash
DRP_TEST_IMAGE="git.example.org:5050/group/test-project/drp-check:$(date +%Y%m%d%H%M%S)"
DRP_TEST_DIR=$(mktemp -d)
printf '%s\n' "$DRP_TEST_IMAGE" > "$DRP_TEST_DIR/check.txt"
tar -C "$DRP_TEST_DIR" -cf - check.txt | docker image import - "$DRP_TEST_IMAGE"
docker push "$DRP_TEST_IMAGE"
docker image rm "$DRP_TEST_IMAGE"
docker pull "$DRP_TEST_IMAGE"
```

Все команды должны завершиться успешно. Образ создаётся без внешнего базового образа
и кэша сборки. Удаление локального образа перед `pull` позволяет проверить скачивание;
сравните digest в выводе `push` и `pull`.
После проверки удалите тестовый тег через UI домашнего GitLab, временный каталог
`$DRP_TEST_DIR` и локальный тестовый образ.

Для обычной эксплуатации резервного экземпляра достаточно чтения, clone и pull.
Push выше — отдельная проверка nginx и Registry при первоначальной настройке.
