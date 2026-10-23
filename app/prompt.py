"""The instructions Gemini gets at the start of every conversation."""
from .booking.speech import say_time


def build_instructions(settings, now):
    """Fill the business details and the current date into the prompt template. Gemini keeps the conversation
    itself, so this text is sent once, in the setup of each new conversation."""
    services = "\n".join(
        f"- {s['name']} (id {s['id']}): {s['duration_minutes']} minutes. {s.get('description', '')}".rstrip()
        for s in settings.services
    )
    values = {
        "BUSINESS_NAME": settings.business_name,
        "STAFF_NAME": settings.business_staff_name,
        "TIMEZONE": settings.business_timezone,
        "HOURS": settings.business_hours_text.replace(";", "; "),
        "BREAKS": settings.business_breaks_text,
        "MIN_NOTICE": str(settings.min_notice_minutes),
        "MAX_ADVANCE_DAYS": str(settings.max_advance_days),
        "CANCELLATION_NOTICE_HOURS": str(settings.cancellation_notice_hours),
        "SERVICES": services,
        "NOW": f"{now.strftime('%A')} {now.day} {now.strftime('%B %Y')}, {say_time(now)}",
    }
    text = settings.system_prompt_template
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)
    return text
