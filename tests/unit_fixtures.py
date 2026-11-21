from datetime import datetime
from zoneinfo import ZoneInfo

TZ = "Europe/London"
# Monday 3 November 2025, 08:00 local: the whole working day is still ahead.
FIXED_NOW = datetime(2025, 11, 3, 8, 0, tzinfo=ZoneInfo(TZ))
