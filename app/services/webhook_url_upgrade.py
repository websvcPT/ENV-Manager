"""Moving a webhook address stored as plain text into the secret setting.

The address used to be an ordinary setting, stored as JSON text under the name it keeps as a
secret. Chat tools put their credential in the address, so it is sealed now. This step runs at
startup and on the working copy of a restored backup, finds a row that still holds readable JSON
text and seals that value in place with the key given. A row that does not parse as JSON is already
a sealed value and is left alone, so the step can run any number of times.
"""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AppSetting
from app.services.app_settings import WEBHOOK_URL_KEY, write_secret
from app.services.audit import AuditActor


async def seal_plain_webhook_url(db: AsyncSession, crypto: CryptoService) -> bool:
    """Seal a plain stored webhook address and commit; returns whether one was found.

    An empty or malformed plain value is dropped instead, since there is nothing to keep. The
    audit entry names the setting and never the address.
    """
    row = await db.get(AppSetting, WEBHOOK_URL_KEY)
    if row is None:
        return False
    try:
        value = json.loads(row.value)
    except ValueError:
        return False
    if isinstance(value, str) and value.strip():
        await write_secret(
            db, crypto, WEBHOOK_URL_KEY, value.strip(), actor=AuditActor.system("upgrade")
        )
    else:
        await db.delete(row)
    await db.commit()
    return True
