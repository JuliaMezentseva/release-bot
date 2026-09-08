#!/usr/bin/env bash
# Первый деплой release-bot на чистый сервер Ubuntu 24.04.
# Запускать на сервере от root или от пользователя с sudo:
#   bash install.sh
# Повторный запуск безопасен: config.env, releases_data.json и media не трогаются.
set -euo pipefail

REPO=${REPO:-https://github.com/JuliaMezentseva/release-bot.git}
APP_DIR=/opt/ld_bot
SITE_DIR=/var/www/digest
APP_USER=${APP_USER:-julia}

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
    as_app() { runuser -u "$APP_USER" -- "$@"; }
else
    SUDO="sudo"
    as_app() { sudo -u "$APP_USER" "$@"; }
fi

echo "==> 1/8 Зависимости"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3-venv python3-pip nginx git

echo "==> 2/8 Пользователь $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || $SUDO useradd -m -s /bin/bash "$APP_USER"

echo "==> 3/8 Код в $APP_DIR"
$SUDO mkdir -p "$APP_DIR"
$SUDO chown "$APP_USER":"$APP_USER" "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    as_app git -C "$APP_DIR" pull --ff-only
else
    as_app git clone -q "$REPO" "$APP_DIR"
fi

echo "==> 4/8 Виртуальное окружение"
[ -d "$APP_DIR/venv" ] || as_app python3 -m venv "$APP_DIR/venv"
as_app "$APP_DIR/venv/bin/pip" install -q --upgrade pip
as_app "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> 5/8 Конфиг с токенами"
if [ -f "$APP_DIR/config.env" ]; then
    echo "    config.env уже есть — не трогаю"
    NEED_TOKENS=0
else
    as_app cp "$APP_DIR/config.env.example" "$APP_DIR/config.env"
    $SUDO chmod 600 "$APP_DIR/config.env"
    NEED_TOKENS=1
fi

echo "==> 6/8 Каталог сайта $SITE_DIR"
$SUDO mkdir -p "$SITE_DIR/media"
$SUDO cp "$APP_DIR/digest_template.html" "$SITE_DIR/"
[ -f "$SITE_DIR/releases_data.json" ] || echo '[]' | $SUDO tee "$SITE_DIR/releases_data.json" >/dev/null
[ -f "$SITE_DIR/index.html" ] || $SUDO cp "$APP_DIR/digest_template.html" "$SITE_DIR/index.html"
$SUDO chown -R "$APP_USER":www-data "$SITE_DIR"
$SUDO chmod -R u+rwX,g+rX "$SITE_DIR"

echo "==> 7/8 Nginx"
$SUDO cp "$APP_DIR/deploy/nginx-digest.conf" /etc/nginx/sites-available/digest
$SUDO ln -sf /etc/nginx/sites-available/digest /etc/nginx/sites-enabled/digest
$SUDO rm -f /etc/nginx/sites-enabled/default
$SUDO nginx -t
$SUDO systemctl reload nginx

echo "==> 8/8 Systemd-сервисы (User=$APP_USER)"
for unit in ld_bot ld_webhook; do
    sed "s/^User=.*/User=$APP_USER/" "$APP_DIR/deploy/$unit.service" \
        | $SUDO tee "/etc/systemd/system/$unit.service" >/dev/null
done
$SUDO systemctl daemon-reload
$SUDO systemctl enable -q ld_bot ld_webhook

if [ "$NEED_TOKENS" = "1" ]; then
    cat <<MSG

  Сервисы установлены, но НЕ запущены: в $APP_DIR/config.env пока заглушки.
  Заполнить:  nano $APP_DIR/config.env
  Запустить:  systemctl start ld_bot ld_webhook

MSG
else
    $SUDO systemctl restart ld_bot ld_webhook
fi
echo "Готово."
