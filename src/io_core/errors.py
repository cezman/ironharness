"""Ошибки политик io-core. Единые имена для всех транспортов и обёрток."""

from __future__ import annotations


class SandboxViolation(Exception):
    """Попытка обратиться за пределы песочницы."""


class QuotaExceeded(Exception):
    """Превышена квота песочницы (байты или число файлов)."""


class RateLimitExceeded(Exception):
    """Превышен лимит операций в окне."""


class OperationTimeout(Exception):
    """Истёк дедлайн операции/сессии."""


class VerificationError(Exception):
    """Проверка эффекта не прошла: ожидание не подтвердилось за отведённое время."""


class ConnectionLost(Exception):
    """Соединение с устройством потеряно (в том числе по сценарию сбоев)."""
