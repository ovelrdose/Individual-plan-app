"""Настройки проекта. Всё, что отличается между машинами, берётся из окружения (.env)."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    TIME_ZONE=(str, "Europe/Moscow"),
    PUBLIC_BOARD_NETWORKS=(list, []),
    SESSION_IDLE_HOURS=(int, 8),
)
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env("DEBUG")
SECRET_KEY = env("SECRET_KEY")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "simple_history",
    "apps.org",
    "apps.accounts",
    "apps.catalog",
    "apps.staff",
    "apps.programs",
    "apps.scheduling",
    "apps.exchange",
    "apps.cards",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Все страницы — только после входа; исключения помечаются @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.department",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

# Сессия завершается после SESSION_IDLE_HOURS часов бездействия (FR-ACC-1):
# срок продлевается при каждом запросе.
SESSION_COOKIE_AGE = env("SESSION_IDLE_HOURS") * 3600
SESSION_SAVE_EVERY_REQUEST = True

# Блокировка входа после серии неудачных попыток (NFR-6).
LOGIN_LOCKOUT_ATTEMPTS = 10
LOGIN_LOCKOUT_MINUTES = 15

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = env("TIME_ZONE")
USE_I18N = True
# Служебные отметки времени (журналы) хранятся в UTC. Даты и время занятий — «наивные»
# локальные DateField/TimeField, часовые пояса к ним не применяются (TZ.md, NFR-3).
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Публичная шахматка (TZ.md, FR-ACC-5). Пустой список — доступ без ограничений.
PUBLIC_BOARD_NETWORKS = env("PUBLIC_BOARD_NETWORKS")
