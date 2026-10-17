"""SQLAlchemy models. Importing this package registers every table on the metadata."""

from app.models.app_setting import AppSetting
from app.models.base import Base

__all__ = ["AppSetting", "Base"]
