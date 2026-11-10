"""Локальный сервер принимает команды только из окна самого приложения."""
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import config
from app.server import app, origin_ok


def test_own_window_allowed():
    assert origin_ok(f"http://127.0.0.1:{config.PORT}")
    assert origin_ok(f"http://localhost:{config.PORT}")


def test_no_origin_is_local_program():
    assert origin_ok(None) and origin_ok("")


def test_foreign_sites_rejected():
    for origin in ("https://evil.example", "http://127.0.0.1:1", f"http://evil.example:{config.PORT}", "null"):
        assert not origin_ok(origin), origin


def test_host_check_enabled():
    assert any(m.cls is TrustedHostMiddleware for m in app.user_middleware)


def test_follows_fallback_port(monkeypatch):
    """Порт 8765 занят → main.py берёт соседний; окно на новом порту должно пускаться, старый — нет."""
    monkeypatch.setattr(config, "PORT", config.PORT + 7)
    assert origin_ok(f"http://127.0.0.1:{config.PORT}")
    assert not origin_ok(f"http://127.0.0.1:{config.PORT - 7}")
