#!/usr/bin/env bash
# Установка и обновление сервера Ruletka на Ubuntu.
# Запуск на сервере:
#   curl -fsSL https://raw.githubusercontent.com/gibrazone707-spec/baraban/main/server/install.sh | bash
# Повторный запуск той же командой обновляет код и перезапускает бота, токен и база сохраняются.
set -euo pipefail

REPO="https://github.com/gibrazone707-spec/baraban"
APP=/opt/ruletka
DATA=/var/lib/ruletka
GAME_URL_DEFAULT="https://gibrazone707-spec.github.io/baraban/"

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Запустите от root"; exit 1; fi
export DEBIAN_FRONTEND=noninteractive

say "Ставлю системные пакеты"
apt-get update -q
apt-get install -y -q git curl python3 python3-venv ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  say "Ставлю Caddy (HTTPS-сертификат получит сам)"
  if curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
     && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' -o /etc/apt/sources.list.d/caddy-stable.list; then
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
  fi
  apt-get install -y -q caddy
fi

say "Скачиваю код игры"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q --depth 1 origin main
  git -C "$APP" reset -q --hard origin/main
else
  rm -rf "$APP"
  git clone -q --depth 1 "$REPO" "$APP"
fi

say "Ставлю Python-библиотеки"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/server/requirements.txt"

mkdir -p "$DATA"
ENV="$DATA/.env"
if [ ! -f "$ENV" ]; then
  echo
  echo "Вставьте токен бота @Ruletka_nomerov_bot из @BotFather и нажмите Enter."
  echo "(Символы не будут видны при вставке, это нормально.)"
  TOKEN=""
  while ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; do
    read -rsp "Токен: " TOKEN </dev/tty; echo
    TOKEN="$(echo -n "$TOKEN" | tr -d '[:space:]')"
    [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || echo "Это не похоже на токен. Он выглядит как 1234567890:AAH... Попробуйте ещё раз."
  done
  echo
  echo "Теперь ваш Telegram-ID (число). Узнать его: откройте в Telegram бота @userinfobot и нажмите Start."
  echo "Только этот аккаунт получит вкладку «Админ» и сможет видеть статистику."
  ADMIN=""
  while ! [[ "$ADMIN" =~ ^[0-9]{5,15}$ ]]; do
    read -rp "Ваш ID: " ADMIN </dev/tty
    ADMIN="$(echo -n "$ADMIN" | tr -d '[:space:]')"
    [[ "$ADMIN" =~ ^[0-9]{5,15}$ ]] || echo "ID состоит только из цифр. Попробуйте ещё раз."
  done
  cat > "$ENV" <<EOF
BOT_TOKEN=$TOKEN
ADMIN_IDS=$ADMIN
GAME_URL=$GAME_URL_DEFAULT
DB_PATH=$DATA/ruletka.db
EOF
  chmod 600 "$ENV"
fi
ln -sfn "$ENV" "$APP/server/.env"

IP="$(curl -4 -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
DOMAIN="${IP//./-}.sslip.io"

say "Настраиваю HTTPS для $DOMAIN"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8080
}
EOF
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi
systemctl enable -q caddy
systemctl restart caddy

say "Запускаю бота как службу"
cat > /etc/systemd/system/ruletka.service <<EOF
[Unit]
Description=Ruletka bot and API
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP/server
ExecStart=$APP/venv/bin/python bot.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q ruletka
systemctl restart ruletka

sleep 6
if systemctl is-active --quiet ruletka; then
  OK_API="нет ответа"
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 5 "https://$DOMAIN/" >/dev/null 2>&1; then OK_API="работает"; break; fi
    sleep 3
  done
  echo
  echo "=============================================="
  echo " ✅ Готово! Бот запущен."
  echo " Адрес сервера: https://$DOMAIN  ($OK_API)"
  echo " Проверьте: напишите боту /stats"
  echo "=============================================="
else
  echo
  echo "❌ Бот не запустился. Последние строки журнала:"
  journalctl -u ruletka -n 30 --no-pager
fi
