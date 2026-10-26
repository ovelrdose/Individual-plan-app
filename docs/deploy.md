# Установка на сервер клиники

Сервис работает на виртуальных машинах: Python в виртуальном окружении (`venv`), gunicorn, nginx и PostgreSQL на той же машине. Docker не используется (решение 73). Сервис отвечает по HTTPS внутри сети клиники.

## Два контура

| Контур | Имя (пример) | Для чего |
|---|---|---|
| **Прод** | `omr4.clinic.local` | Рабочий сервис: врачи и специалисты ФР работают только здесь |
| **Тест** | `omr4-test.clinic.local` | Проверка новой версии перед выкатом на прод: миграции на настоящих данных, приёмка экранов |

Каждый контур — **отдельная ВМ** со своей базой, своим `.env` и своим сертификатом. Контуры ничего не знают друг о друге: у каждого своя база, обновления страниц в реальном времени (`/events/`) идут внутри своей базы. Новая версия всегда идёт **сначала на тест, потом на прод** (раздел 12).

Обе ВМ ставятся по одним и тем же шагам 0–11. Ниже `omr4.clinic.local` — имя прода; на тесте подставьте `omr4-test.clinic.local` во всех местах: `.env`, nginx, сертификат. Чем тест отличается от прода:

| Что | Прод | Тест |
|---|---|---|
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, сертификат | `omr4.clinic.local` | `omr4-test.clinic.local` |
| `SECRET_KEY` | свой | **другой**, не копировать с прода |
| Пароль роли PostgreSQL (`DATABASE_URL`) | свой | другой |
| База | рабочая | копия прода перед проверкой версии (раздел 12) |
| Бэкап по таймеру (шаг 10) | обязательно | не нужен: тест восстанавливается из копии прода |
| `PUBLIC_BOARD_NETWORKS` | сети клиники | только сеть принимающих (не пусто: пусто — `/board/` открыт всем) |
| Кто входит | все сотрудники | администратор и те, кто принимает версию |

- **На тесте — персональные данные.** Копия прода содержит ФИО пациентов, поэтому тестовую ВМ защищайте так же, как прод: доступ только из сети клиники, права на файлы и копии — как на проде (152-ФЗ). Пускать на тест кого-то ещё — только тех, у кого есть доступ и к проду.
- **Ограничьте доступ к тесту в nginx** списком адресов принимающих: `allow 10.0.5.0/24; deny all;` в блоке `server` на 443 (адреса — ваши). `PUBLIC_BOARD_NETWORKS` на тесте — та же сеть.
- **Пароли на тесте — как на проде.** После восстановления копии пользователи входят на тест со своими рабочими паролями. Скажите принимающим, что тест — **не для работы**: всё, что там сделано, пропадёт при следующем восстановлении копии и на прод не попадёт. Отличить контуры можно по адресу в браузере.

Все конфиги лежат в каталоге `deploy/`, они одинаковые для обоих контуров:

| Файл | Куда | Что это |
|---|---|---|
| `deploy/env.production.example` | `/opt/omr4/app/.env` | Настройки сервиса |
| `deploy/gunicorn.conf.py` | используется на месте | gunicorn: 3 воркера × 64 потока |
| `deploy/omr4.service` | `/etc/systemd/system/` | Служба gunicorn |
| `deploy/nginx-omr4.conf` | `/etc/nginx/sites-available/omr4` | HTTPS, статика, поток `/events/` |
| `deploy/omr4-backup.sh`, `.service`, `.timer` | `/etc/systemd/system/` (два последних) | Ежедневный `pg_dump`, 30 дней |

Раскладка на сервере (одинаковая на обеих ВМ):

```
/opt/omr4/app        код (git clone или архив), .env, staticfiles/
/opt/omr4/venv       виртуальное окружение Python
/var/backups/omr4    копии базы
```

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

Код должен быть закоммичен: на сервер попадает только то, что лежит в git. Каталог `primary_docs/` (исходные документы заказчика, в них есть персональные данные) в git не входит — его копируют отдельно, см. «Исходные документы заказчика» ниже.

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

### Исходные документы заказчика (`primary_docs/`)

Нужны только командам загрузки: `load_initial_catalog` (`lfk.xlsx`, `basseyn.xlsx`, `Gruppy_DS.xlsx`), `load_board_instructors` (`instructions_schedule.xlsx`), `load_current_patients` (`Svodka_*.docx`) и `extract_card_templates` (`programm_template.xlsx`). Работающему сервису они не нужны: справочники уже в базе, шаблоны карт лежат в коде. Если база переносится копией (шаг 7) и команды загрузки на сервере не запускаются, этот шаг можно пропустить.

```bash
# на машине разработки
tar -czf primary_docs.tar.gz primary_docs
scp primary_docs.tar.gz admin@omr4.clinic.local:/tmp/
shred -u primary_docs.tar.gz

# на сервере
sudo -u omr4 tar -xzf /tmp/primary_docs.tar.gz -C /opt/omr4/app
sudo chmod -R go-rwx /opt/omr4/app/primary_docs     # персональные данные — только пользователю omr4
sudo shred -u /tmp/primary_docs.tar.gz
```

Каталог в `.gitignore`, поэтому `git pull` его не трогает. Исключение — первое обновление с версии, где `primary_docs/` ещё лежал в git: pull удалит файлы, которые раньше были в репозитории. Тогда после pull скопируйте каталог заново.

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

Этот шаг — для **первой установки прода**. Тест после первой установки получает копию прода (раздел 12, «Копия прода на тест»), а не базу разработки.

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
| `doctor`, `rehab` | `seed_dev`, вымышленные «Иванова А.А.» и «Петрова Б.Б.», пароль `devpass123` | Задать пароль или отключить на экране «Пользователи» (снять «Учётная запись действует») |
| `nechunaeva`, `petlina`, `andrusenko`, `frolova` | Врачи из сводок, без пароля | `manage.py changepassword <логин>` и передать врачам |

Специалистов ФР с настоящими фамилиями заведите на экране «Пользователи» («Добавить пользователя», шаблон «Специалист ФР», отделение ОМР № 4) или командой `create_users --rehab <логин> --rehab-name 'Фамилия И.О.'`.

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
$PY load_initial_catalog                                   # справочники из primary_docs/*.xlsx (шаг 3)
$PY create_users                                           # врач, специалист ФР, администратор
$PY load_board_instructors --since 2026-09-01 --replace    # инструкторы, пары, распорядок
# Текущие пациенты — сводки Svodka_*.docx в primary_docs/, затем:
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

Только на проде. На тесте таймер не включают: тест восстанавливается из копии прода (раздел 12).

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
2. Вход под `admin` с новым паролем: в списке программ — те же пациенты, что при разработке (на тесте — что на проде).
3. Меню **«Расписание»**: сетка как при разработке (или составленная на сегодня из последней).
4. Откройте расписание в двух браузерах, перенесите пациента в одном — второй обновится сам за 1–2 секунды. Если нет, проверьте блок `location /events/` в nginx.
5. «Скачать карту» у любого пациента — файл открывается в MS Excel.
6. `/board/` с компьютера из сети клиники открывается, из других сетей — 404.
7. `journalctl -u omr4 --since today` — без `ERROR`.

## 12. Выпуск версии: тест → прод

Новая версия всегда сначала проверяется на тесте **на свежей копии прода**: так проверяются и миграции на настоящих данных, и экраны. На прод выкатывается **тот же коммит**, что проверили.

### Подготовка версии (машина разработки)

```bash
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/python manage.py makemigrations --check --dry-run   # «No changes detected»
.venv/bin/pytest                                              # все тесты зелёные
git tag -a v2026.10.26 -m "Справочники в вебе, пометки тренажёров"   # номер — дата выпуска
git push origin main --tags
```

Если менялись зависимости в `pyproject.toml`, сначала пересоберите `requirements.txt` из группы `prod` и закоммитьте его.

### Копия прода на тест

```bash
# на проде
sudo systemctl start omr4-backup.service                  # свежая копия
ls -t /var/backups/omr4/ | head -1                        # её имя
sudo scp /var/backups/omr4/omr4-2026-10-26_1500.dump admin@omr4-test.clinic.local:/tmp/

# на тесте
sudo systemctl stop omr4
sudo -u postgres dropdb omr4
sudo -u postgres createdb -O omr4 -E UTF8 -T template0 omr4
sudo chown postgres /tmp/omr4-*.dump
sudo -u postgres pg_restore --no-owner --role=omr4 --dbname=omr4 /tmp/omr4-*.dump
sudo shred -u /tmp/omr4-*.dump
```

Сервис на тесте пока не запускайте: база на старой версии, её поднимет `migrate` на следующем шаге.

### Выкат на тест

```bash
cd /opt/omr4/app
sudo -u omr4 git fetch --tags && sudo -u omr4 git checkout v2026.10.26   # или распаковать архив версии
sudo -u omr4 /opt/omr4/venv/bin/pip install -r requirements.txt
sudo -u omr4 /opt/omr4/venv/bin/python manage.py migrate                 # миграции на копии прода
sudo -u omr4 /opt/omr4/venv/bin/python manage.py collectstatic --noinput
sudo -u omr4 /opt/omr4/venv/bin/python manage.py check --deploy
sudo systemctl start omr4
```

Затем — проверка из раздела 11 и приёмка того, что изменилось в версии. Ошибка `migrate` на тесте — повод остановиться: прод не трогаем, исправляем версию и повторяем с новой копией.

### Выкат на прод

Тот же тег, что приняли на тесте. Лучше в начале или в конце рабочего дня: на время `migrate` и перезапуска (обычно меньше минуты) сервис недоступен.

```bash
sudo systemctl start omr4-backup.service                   # копия перед обновлением
cd /opt/omr4/app
sudo -u omr4 git fetch --tags && sudo -u omr4 git checkout v2026.10.26
sudo -u omr4 /opt/omr4/venv/bin/pip install -r requirements.txt
sudo -u omr4 /opt/omr4/venv/bin/python manage.py migrate
sudo -u omr4 /opt/omr4/venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart omr4
journalctl -u omr4 --since "5 min ago"                     # без ERROR
```

Открытые вкладки переподключатся сами через несколько секунд.

### Откат прода

Если после выката что-то сломалось и быстро не исправить: верните предыдущий тег и базу из копии, сделанной перед обновлением.

```bash
sudo systemctl stop omr4
cd /opt/omr4/app && sudo -u omr4 git checkout v2026.10.20          # предыдущая версия
sudo -u omr4 /opt/omr4/venv/bin/pip install -r requirements.txt
# база — из копии перед обновлением (раздел 10, «Восстановление из копии»), без migrate новой версии
sudo -u omr4 /opt/omr4/venv/bin/python manage.py collectstatic --noinput
sudo systemctl start omr4
```

Всё, что сотрудники успели сделать между выкатом и откатом, при этом пропадёт: откат — крайняя мера.

### Разовые шаги отдельных версий

- Сервер ставился до поддержки `.doc` (16.10.2026) — один раз доустановите LibreOffice: `sudo apt install -y --no-install-recommends libreoffice-writer-nogui`. Проверка: `soffice --version`.
- Версия со справочниками в вебе (решение 83, 26.10.2026): миграция `accounts.0004` снимает «Статус персонала» у всех, кроме суперпользователей. Админка Django после неё открывается только суперпользователю (`admin`), остальные ведут справочники на экранах «Справочники», «Групповые занятия», «Пользователи».

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
| `migrate` на проде просит миграции, которых не было на тесте | На прод попал не тот коммит: сверьте тег (`git describe --tags`) |
| «Сегодня» в расписании не тот день | Часовой пояс системы не совпадает с `TIME_ZONE` |
| Нет стилей | Не выполнен `collectstatic` или у nginx нет прав на `/opt/omr4/app/staticfiles` |
