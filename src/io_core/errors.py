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


class TransportClosedError(RuntimeError):
    """Операция на транспорте, который не открыт (или уже закрыт). Заменяет
    голый assert: обязан работать и под python -O, который assert'ы вырезает."""


class JournalCorrupt(Exception):
    """Журнал повреждён: строка не JSON-объект, отсутствуют служебные поля или
    data_hex не парсится. Честный отказ с именем файла вместо KeyError/AttributeError."""


class PolicyViolation(PermissionError):
    """Operation denied by the access policy (host allowlist or enabled kinds)."""
