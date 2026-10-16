# Установка на сервер клиники

Рабочая установка на виртуальной машине: Python в виртуальном окружении (`venv`), gunicorn, nginx, PostgreSQL на той же машине. Docker не используется (решение 73). Цель — сервис, который отвечает по HTTPS внутри сети клиники, и **база в том же состоянии, что на машине разработки**.

Все конфиги лежат в каталоге `deploy/`:

| Файл | Куда | Что это |
|---|---|---|
| `deploy/env.production.example` | `/opt/omr4/app/.env` | Настройки сервиса |
| `deploy/gunicorn.conf.py` | используется на месте | gunicorn: 3 воркера × 64 потока |
| `deploy/omr4.service` | `/etc/systemd/system/` | Служба gunicorn |
| `deploy/nginx-omr4.conf` | `/etc/nginx/sites-available/omr4` | HTTPS, статика, поток `/events/` |
| `deploy/omr4-backup.sh`, `.service`, `.timer` | `/etc/systemd/system/` (два последних) | Ежедневный `pg_dump`, 30 дней |

Раскладка на сервере:

```
/opt/omr4/app        код (git clone или архив), .env, staticfiles/
/opt/omr4/venv       виртуальное окружение Python
/var/backups/omr4    копии базы
```

Ниже `omr4.clinic.local` — имя сервера в сети клиники. Подставьте своё во всех местах: `.env`, nginx, сертификат.

---

## 0. Виртуальная машина

- **ОС:** Ubuntu Server 24.04 LTS. В ней по умолчанию Python 3.12 и PostgreSQL 16, то есть ровно то, что нужно. В Debian 12 Python 3.11 — он не подходит.
- **Ресурсы:** 2 vCPU, 4 ГБ RAM, 20 ГБ диска. Под копии базы на старте хватит 1–2 ГБ.
- **Сеть:** статический IP и DNS-имя в сети клиники. Доступ из интернета не нужен. Входящие порты — 443 и 80 (с 80 идёт перенаправление на 443), плюс 22 для администрирования.

## 1. Пакеты и система

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
    python3.12 python3.12-venv python3-pip \
    postgresql-16 postgresql-client-16 \
    nginx \
    git openssl ca-certificates tzdata
# LibreOffice без графики — только чтобы читать листы назначений в старом формате .doc
# (apps/exchange/word_doc.py пересохраняет их в .docx). Без него .doc не загрузится,
# врач увидит просьбу сохранить файл как .docx; всё остальное работает.
sudo apt install -y --no-install-recommends libreoffice-writer-nogui

# Часовой пояс системы = TIME_ZONE в .env: «сегодня» в расписании считается по нему.
sudo timedatectl set-timezone Europe/Moscow
timedatectl            # проверить, что NTP synchronized: yes

sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```

Компиляторы и `-dev`-пакеты не нужны: `psycopg[binary]`, `lxml` и `pillow` ставятся готовыми колёсами. Если у ВМ нет выхода в интернет, см. раздел «Без интернета» в конце.

## 2. Пользователь и каталоги

```bash
sudo adduser --system --group --home /opt/omr4 --shell /usr/sbin/nologin omr4
sudo install -d -o omr4 -g omr4 -m 755 /opt/omr4
sudo install -d -o postgres -g postgres -m 700 /var/backups/omr4
```

## 3. Код

Код должен быть закоммичен: на сервер попадает только то, что лежит в git. Сводки пациентов (`primary_docs/Svodka_*.docx`) в git не попадают — так и должно быть.

**Вариант А — с GitHub** (у ВМ есть доступ, на GitHub добавлен deploy key):

```bash
sudo -u omr4 git clone git@github.com:ovelrdose/Individual-plan-app.git /opt/omr4/app
```

**Вариант Б — архивом с машины разработки:**

```bash
# на машине разработки
git archive --format=tar.gz -o omr4-app.tar.gz HEAD
scp omr4-app.tar.gz admin@omr4.clinic.local:/tmp/

# на сервере
sudo -u omr4 mkdir /opt/omr4/app
sudo -u omr4 tar -xzf /tmp/omr4-app.tar.gz -C /opt/omr4/app
```

## 4. PostgreSQL

```bash
# Пароль — длинный случайный, он же пойдёт в DATABASE_URL.
sudo -u postgres psql -c "CREATE ROLE omr4 LOGIN PASSWORD 'СЛОЖНЫЙ_ПАРОЛЬ';"
sudo -u postgres psql -c "CREATE DATABASE omr4 OWNER omr4 ENCODING 'UTF8' TEMPLATE template0;"
```

- PostgreSQL по умолчанию слушает только `localhost`, менять это не нужно.
- Расширение `btree_gist`, которое нужно ограничениям «пациент не в двух местах», создаётся при восстановлении копии или миграцией. В PostgreSQL 16 оно доверенное, суперпользователь не нужен.
- `max_connections` по умолчанию 100, этого хватает: поток обновлений `/events/` соединение с базой не держит.

## 5. Виртуальное окружение и пакеты Python

```bash
sudo -u omr4 python3.12 -m venv /opt/omr4/venv
sudo -u omr4 /opt/omr4/venv/bin/pip install --upgrade pip
sudo -u omr4 /opt/omr4/venv/bin/pip install -r /opt/omr4/app/requirements.txt
```

`requirements.txt` содержит все пакеты прода с точными версиями, включая вложенные: Django, django-environ, django-simple-history, psycopg, openpyxl, python-docx, pillow, gunicorn и их зависимости. Он собирается из группы `prod` в `pyproject.toml`. Пакеты для тестов (pytest, ruff) на сервер не ставятся.

## 6. Настройки `.env`

```bash
sudo -u omr4 cp /opt/omr4/app/deploy/env.production.example /opt/omr4/app/.env
sudo -u omr4 chmod 600 /opt/omr4/app/.env
sudo -u omr4 /opt/omr4/venv/bin/python -c "import secrets; print(secrets.token_urlsafe(50))"
sudo -u omr4 nano /opt/omr4/app/.env
```

| Переменная | Значение на проде |
|---|---|
| `DEBUG` | `false` |
| `SECRET_KEY` | Результат команды выше |
| `ALLOWED_HOSTS` | `omr4.clinic.local` (через запятую можно добавить IP) |
| `CSRF_TRUSTED_ORIGINS` | `https://omr4.clinic.local` |
| `DATABASE_URL` | `postgres://omr4:СЛОЖНЫЙ_ПАРОЛЬ@localhost:5432/omr4` |
| `TIME_ZONE` | Как у системы (шаг 1) |
| `PUBLIC_BOARD_NETWORKS` | Сети клиники для `/board/`, например `10.0.0.0/8` |
| `BEHIND_PROXY` | `true` — сервис за nginx |
| `SECURE_HSTS_SECONDS` | `0`, пока сертификат самоподписанный |

Проверка:

```bash
cd /opt/omr4/app
sudo -u omr4 /opt/omr4/venv/bin/python manage.py check --deploy
```

Допустимо одно предупреждение — `security.W004` (HSTS), пока `SECURE_HSTS_SECONDS=0`.

## 7. База в том же состоянии, что при разработке

Переносится копия рабочей базы разработки: пациенты, назначения, расписание, инструкторы, смены, распорядок, справочники, пользователи и журнал изменений. Повторять команды загрузки не нужно.

**На машине разработки** (остановите `runserver`, чтобы копия была согласованной):

```bash
.venv/bin/python manage.py migrate        # база — на той же версии, что код
pg_dump --format=custom --no-owner --no-acl \
    --file=omr4-$(date +%F).dump postgres://omr4:omr4@localhost:5432/omr4
scp omr4-$(date +%F).dump admin@omr4.clinic.local:/tmp/
```

**На сервере:**

```bash
sudo chown postgres /tmp/omr4-*.dump
sudo -u postgres pg_restore --no-owner --role=omr4 --dbname=omr4 /tmp/omr4-*.dump

cd /opt/omr4/app
sudo -u omr4 /opt/omr4/venv/bin/python manage.py migrate          # «No migrations to apply»
sudo -u omr4 /opt/omr4/venv/bin/python manage.py collectstatic --noinput

sudo shred -u /tmp/omr4-*.dump      # в копии персональные данные пациентов
```

Если в базе разработки уже есть код новее, чем на сервере, `migrate` покажет непримененные миграции — это значит, что код на сервер попал не тот.

**Пользователи после переноса** — обязательно:

| Логин | Откуда | Что сделать |
|---|---|---|
| `admin` | `seed_dev`, пароль `devpass123` | `manage.py changepassword admin` |
| `doctor`, `rehab` | `seed_dev`, вымышленные «Иванова А.А.» и «Петрова Б.Б.», пароль `devpass123` | Задать пароль или отключить в админке (снять «Активный») |
| `nechunaeva`, `petlina`, `andrusenko`, `frolova` | Врачи из сводок, без пароля | `manage.py changepassword <логин>` и передать врачам |

Специалистов ФР с настоящими фамилиями заведите в админке («Пользователи» → «Добавить», членство «Специалист ФР» в ОМР № 4) или командой `create_users --rehab <логин> --rehab-name 'Фамилия И.О.'`.

```bash
sudo -u omr4 /opt/omr4/venv/bin/python manage.py changepassword admin
```

Расписание в копии составлено на день, когда её сделали. Если копия переносится позже, при первом открытии система сама составит расписание на сегодня и на следующий будний день из последнего составленного. Выписанные к этому дню уйдут, остальные останутся на местах.

### Вместо копии — установка с нуля

Если нужно собрать базу заново, а не переносить, выполните команды в таком порядке:

```bash
cd /opt/omr4/app
PY="sudo -u omr4 /opt/omr4/venv/bin/python manage.py"
$PY migrate
$PY load_initial_catalog                                   # справочники из primary_docs/*.xlsx
$PY create_users                                           # врач, специалист ФР, администратор
$PY load_board_instructors --since 2026-09-01 --replace    # инструкторы, пары, распорядок
# Текущие пациенты — сводки положить в primary_docs/ (их нет в git), затем:
$PY load_current_patients                                  # только на сегодня/следующий будний день
$PY collectstatic --noinput
```

Так получится то же, но **без ручных правок**, сделанных на сайте после загрузки: смен, распорядка, пометок, переносов. Поэтому для того же состояния переносите копию.

## 8. Служба gunicorn

```bash
sudo cp /opt/omr4/app/deploy/omr4.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omr4
sudo systemctl status omr4
curl -sI -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/accounts/login/ | head -1   # 200 OK
```

Журнал: `journalctl -u omr4 -f`. Ошибки Django с трассировкой пишутся туда же. Журнала доступа у gunicorn нет: в адресах бывает поиск по ФИО.

## 9. nginx и HTTPS

**Сертификат.** Лучше взять сертификат от CA клиники на имя `omr4.clinic.local`. Если CA нет, сделайте самоподписанный:

```bash
sudo install -d -m 755 /etc/ssl/omr4
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout /etc/ssl/omr4/omr4.key -out /etc/ssl/omr4/omr4.crt \
    -subj "/CN=omr4.clinic.local" \
    -addext "subjectAltName=DNS:omr4.clinic.local,IP:10.0.0.10"
sudo chmod 600 /etc/ssl/omr4/omr4.key
```

Самоподписанный `omr4.crt` нужно добавить в доверенные на компьютерах клиники (групповой политикой), иначе браузер будет предупреждать.

**Сайт:**

```bash
sudo cp /opt/omr4/app/deploy/nginx-omr4.conf /etc/nginx/sites-available/omr4
sudo sed -i 's/omr4.clinic.local/ВАШЕ_ИМЯ/g' /etc/nginx/sites-available/omr4
sudo ln -s /etc/nginx/sites-available/omr4 /etc/nginx/sites-enabled/omr4
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Что важно в конфиге:

- для `/events/` отключена буферизация и стоит таймаут 1 час — иначе страницы не будут обновляться сами;
- nginx передаёт `X-Real-IP` (по нему пускается публичное расписание) и `X-Forwarded-Proto`;
- в журнале nginx адрес пишется без строки запроса;
- `client_max_body_size 6m` — под листы назначений до 5 МБ.

nginx (пользователь `www-data`) читает статику из `/opt/omr4/app/staticfiles/`. Каталоги `/opt/omr4` и `/opt/omr4/app` должны быть с правами `755`, а `.env` — `600`.

## 10. Резервные копии

```bash
sudo cp /opt/omr4/app/deploy/omr4-backup.service /opt/omr4/app/deploy/omr4-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omr4-backup.timer
sudo systemctl start omr4-backup.service && journalctl -u omr4-backup -n 5    # первая копия сразу
ls -lh /var/backups/omr4/
```

- **Расписание:** каждый день в 02:30, хранятся 30 дней (`BACKUP_DAYS` в `omr4-backup.sh`).
- **Копии на другой носитель.** Копии на той же ВМ не спасут от потери диска: настройте их вывоз на сервер резервного копирования клиники (rsync, снапшоты ВМ, средства гипервизора).
- **Права.** Копии содержат персональные данные: каталог `700`, файлы `600`, владелец `postgres`.

**Восстановление из копии:**

```bash
sudo systemctl stop omr4
sudo -u postgres dropdb omr4
sudo -u postgres createdb -O omr4 -E UTF8 -T template0 omr4
sudo -u postgres pg_restore --no-owner --role=omr4 --dbname=omr4 /var/backups/omr4/omr4-2026-10-20_0230.dump
cd /opt/omr4/app && sudo -u omr4 /opt/omr4/venv/bin/python manage.py migrate
sudo systemctl start omr4
```

## 11. Проверка после установки

1. `https://omr4.clinic.local/` открывает страницу входа, сертификат принят.
2. Вход под `admin` с новым паролем: в списке программ — те же пациенты, что при разработке.
3. Меню **«Расписание»**: сетка как при разработке (или составленная на сегодня из последней).
4. Откройте расписание в двух браузерах, перенесите пациента в одном — второй обновится сам за 1–2 секунды. Если нет, проверьте блок `location /events/` в nginx.
5. «Скачать карту» у любого пациента — файл открывается в MS Excel.
6. `/board/` с компьютера из сети клиники открывается, из других сетей — 404.
7. `journalctl -u omr4 --since today` — без `ERROR`.

## 12. Обновление версии

```bash
sudo systemctl start omr4-backup.service                   # копия перед обновлением
cd /opt/omr4/app
sudo -u omr4 git pull                                      # или распаковать новый архив
sudo -u omr4 /opt/omr4/venv/bin/pip install -r requirements.txt
sudo -u omr4 /opt/omr4/venv/bin/python manage.py migrate
sudo -u omr4 /opt/omr4/venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart omr4
```

Открытые вкладки переподключатся сами через несколько секунд.

Если сервер ставился до поддержки `.doc` (16.10.2026), один раз доустановите LibreOffice: `sudo apt install -y --no-install-recommends libreoffice-writer-nogui`. Проверка: `soffice --version`.

## Без интернета

Если у ВМ нет выхода в интернет, соберите пакеты на машине с интернетом и той же версией Python (3.12, Linux x86_64):

```bash
pip download -r requirements.txt -d wheels/      # затем перенести wheels/ на сервер
# на сервере:
/opt/omr4/venv/bin/pip install --no-index --find-links=/tmp/wheels -r requirements.txt
```

Пакеты Ubuntu берутся из локального зеркала клиники или с установочного образа.

## Частые проблемы

| Признак | Причина и решение |
|---|---|
| 400 Bad Request | Имени сервера нет в `ALLOWED_HOSTS` |
| 403 CSRF при входе | `CSRF_TRUSTED_ORIGINS` не совпадает с адресом в браузере (схема `https://` и имя) |
| Бесконечный редирект на https | В nginx нет `X-Forwarded-Proto` или в `.env` не стоит `BEHIND_PROXY=true` |
| `/board/` отвечает 404 из сети клиники | Сети нет в `PUBLIC_BOARD_NETWORKS` или nginx не передаёт `X-Real-IP` |
| Страницы не обновляются сами | Буферизация `/events/` в nginx или промежуточный прокси |
| «Сегодня» в расписании не тот день | Часовой пояс системы не совпадает с `TIME_ZONE` |
| Нет стилей | Не выполнен `collectstatic` или у nginx нет прав на `/opt/omr4/app/staticfiles` |
