# Развёртывание на сервере (VPS)

Практическое руководство: поднять сборщик + дашборд на Linux-сервере (VPS),
закрыть логином и HTTPS, защитить сам сервер.

---

## 1. Требования к серверу

Приложение лёгкое: Python + SQLite, примерно 1 запрос/сек к CSFloat, дашборд на
Flask. Хватает самого дешёвого VPS.

| Ресурс | Минимум | Комфортно |
|--------|---------|-----------|
| CPU    | 1 vCPU  | 1–2 vCPU  |
| RAM    | 512 МБ  | 1 ГБ      |
| Диск   | 10 ГБ   | 20 ГБ SSD (история продаж растёт медленно) |
| ОС     | Ubuntu 22.04/24.04 LTS (рекомендую) | то же |
| Сеть   | обычный публичный IPv4 | + свой домен для HTTPS |

Провайдеры: Hetzner, Timeweb, Aeza, Vultr, DigitalOcean — любой «за пару $ в
месяц». Нужен исходящий HTTPS к `csfloat.com` и `api.telegram.org`.

> Windows-сервер тоже возможен (сервисы через NSSM), но Linux дешевле, стабильнее
> для фонового демона и проще с HTTPS. Дальше — под Ubuntu.

---

## 2. Первичная настройка сервера (безопасность в первую очередь)

Зайди по SSH под root (данные даёт провайдер), затем:

```bash
# 2.1 Обновить систему
apt update && apt upgrade -y

# 2.2 Создать отдельного пользователя (не работать под root)
adduser csfloat
usermod -aG sudo csfloat

# 2.3 Скопировать свой SSH-ключ этому пользователю (с локальной машины):
#     ssh-copy-id csfloat@IP_СЕРВЕРА
#     (или вручную положить публичный ключ в /home/csfloat/.ssh/authorized_keys)
```

**Отключи вход по паролю по SSH** (только по ключу) — правь `/etc/ssh/sshd_config`:
```
PasswordAuthentication no
PermitRootLogin no
```
затем `systemctl restart ssh`. **Проверь**, что заходишь по ключу под `csfloat`,
прежде чем закрывать текущую сессию.

**Файрвол** — пускаем только SSH и веб:
```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```
Порт дашборда **5000 наружу не открываем** — он слушает только localhost, наружу
смотрит обратный прокси (Caddy) на 80/443.

**Защита от перебора SSH** (по желанию, полезно):
```bash
sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban
```

---

## 3. Установка приложения

Дальше — под пользователем `csfloat`:

```bash
sudo apt install -y python3 python3-venv python3-pip git

git clone https://github.com/FluffyDog000/csfloatpricesparcing.git
cd csfloatpricesparcing

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp items.example.yaml items.yaml   # начальный список (потом правится через веб)
```

Заполни `.env` (см. `README.md`): cookie CSFloat, Telegram-токен/chat_id, и
**обязательно логин** (`DASHBOARD_PASSWORD_HASH` через `python set_password.py`,
`FLASK_SECRET_KEY`). Закрой доступ к секретам:
```bash
chmod 600 .env
```

Проверь разово вручную:
```bash
python run_collector.py --once     # должен собрать данные
python webapp.py                   # Ctrl+C после проверки, что стартует
```

---

## 4. Автозапуск через systemd (два сервиса)

Создай `/etc/systemd/system/csfloat-collector.service`:
```ini
[Unit]
Description=CSFloat sales collector
After=network-online.target
Wants=network-online.target

[Service]
User=csfloat
WorkingDirectory=/home/csfloat/csfloatpricesparcing
ExecStart=/home/csfloat/csfloatpricesparcing/.venv/bin/python run_collector.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

И `/etc/systemd/system/csfloat-web.service`:
```ini
[Unit]
Description=CSFloat dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=csfloat
WorkingDirectory=/home/csfloat/csfloatpricesparcing
ExecStart=/home/csfloat/csfloatpricesparcing/.venv/bin/python webapp.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Включить и запустить:
```bash
### Разовые команды на сервере

Сервисы запускаются через окружение проекта, и ручные команды — тоже. В системе
команды `python` нет, поэтому:

```bash
cd ~/csfloatpricesparcing
.venv/bin/python db_stats.py          # что занимает место в базе
.venv/bin/python check_proxy.py "строка-прокси"
.venv/bin/python report.py --list
```

sudo systemctl daemon-reload
sudo systemctl enable --now csfloat-collector csfloat-web
sudo systemctl status csfloat-collector csfloat-web   # проверить, что active (running)
```
Логи: `journalctl -u csfloat-collector -f` (и `-u csfloat-web`).

> Дашборд должен слушать localhost — в `.env` оставь `CSFLOAT_WEB_HOST=127.0.0.1`.
> Наружу его отдаёт Caddy (следующий шаг).

---

## 5. HTTPS через Caddy (обязательно для публичного доступа)

Без HTTPS логин/пароль уходят по сети открытым текстом. Caddy сам получает и
продлевает бесплатный TLS-сертификат Let's Encrypt.

Нужен домен (или поддомен), A-запись которого указывает на IP сервера.

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:
```
dashboard.example.com {
    reverse_proxy 127.0.0.1:5000
}
```
(подставь свой домен). Затем:
```bash
sudo systemctl reload caddy
```
Открой `https://dashboard.example.com` — увидишь форму входа, соединение по HTTPS.

Приложение читает реальный IP клиента из заголовка `X-Forwarded-For`, который
ставит Caddy — блокировка перебора логина считается по настоящему IP.

---

## 6. Чеклист безопасности

- [ ] Вход по SSH — **только по ключу**, пароль и root-логин отключены.
- [ ] `ufw` включён: наружу только 22/80/443, порт 5000 закрыт.
- [ ] `DASHBOARD_PASSWORD_HASH` задан → дашборд требует логин (проверь: заходит на
      `/login`). Пароль — только как хэш.
- [ ] `FLASK_SECRET_KEY` задан (сессии переживают перезапуск).
- [ ] Дашборд за **HTTPS** (Caddy), сам слушает `127.0.0.1`.
- [ ] `.env` с правами `600`, не в git (`.gitignore`), секреты нигде не
      захардкожены.
- [ ] (Опц.) `CSFLOAT_ADMIN_TOKEN` — доп. слой на изменение предметов/бэкапы.
- [ ] Включён ежедневный бэкап БД в Telegram (страница «Настройки»).
- [ ] `fail2ban` для SSH (по желанию).
- [ ] Регулярно `apt upgrade` (или `unattended-upgrades` для авто-обновлений).

---

## 7. Обновление кода на сервере

```bash
cd ~/csfloatpricesparcing
git pull
source .venv/bin/activate && pip install -r requirements.txt   # если менялись зависимости
sudo systemctl restart csfloat-collector csfloat-web
```
`items.yaml` и `.env` — личные (в `.gitignore`), при `git pull` не конфликтуют.

---

## 8. Бэкапы

- Автоматически: ежедневный экспорт всей БД в Telegram (см. `README.md` →
  «Бэкап…»). Файлы-снапшоты также лежат в `data/backups/`.
- Восстановление: загрузить `*.db` на странице «Настройки» **или** прислать файл
  боту в Telegram (принимается только с твоего `chat_id`).
- Дополнительно можно копировать `data/` на другой хост (`rsync`/`scp`) по cron.
