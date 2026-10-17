"""Store and verify the key check record that detects a wrong master key at startup."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models.app_setting import KEY_CHECK_SETTING, AppSetting


async def ensure_key_check(session: AsyncSession, crypto: CryptoService) -> bool:
    """Verify the stored record, or create it on first start.

    Returns True when the record was created. Raises ``KeyMismatchError`` when a record exists
    that was not sealed with the configured key.
    """
    record = await session.get(AppSetting, KEY_CHECK_SETTING)
    if record is None:
        session.add(AppSetting(key=KEY_CHECK_SETTING, value=crypto.make_key_check()))
        await session.commit()
        return True
    crypto.verify_key_check(record.value)
    return False
