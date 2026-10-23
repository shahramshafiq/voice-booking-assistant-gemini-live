"""A free check of the Google Calendar settings. It does not use Gemini.

Run it from the project folder:
    python -m scripts.check_calendar            checks the token and the calendar ID (read only)
    python -m scripts.check_calendar --write    also creates a test event and deletes it again
"""
import asyncio
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.booking.calendar_client import CalendarClient, CalendarError
from app.config import settings


async def main(check_write):
    client = CalendarClient(settings)
    try:
        await client.check_access()
        print("OK: the token is accepted and the calendar can be read.")

        if check_write:
            start = (datetime.now(ZoneInfo(settings.business_timezone)) + timedelta(days=60)).replace(hour=3, minute=0, second=0, microsecond=0)
            event = await client.insert_event({
                "summary": "Connection test (safe to delete)",
                "start": {"dateTime": start.isoformat(), "timeZone": settings.business_timezone},
                "end": {"dateTime": (start + timedelta(minutes=15)).isoformat(), "timeZone": settings.business_timezone},
            })
            print("OK: a test event was created.")
            await client.delete_event(event["id"])
            print("OK: the test event was deleted again. Writing works.")
    except CalendarError as error:
        print(f"FAILED ({error.code}): {error.message} {getattr(error, 'hint', '')}".strip())
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main("--write" in sys.argv))