"""Turns dates, times and numbers into words that sound natural when a voice reads them aloud,
and turns the spoken digits back into plain numbers for the page and the conversation history."""
import re

# the two shapes a number takes when it is read one digit at a time
GROUPED_DIGITS = r"(?:\d(?: \d){2,3}, )+\d(?: \d){0,3}"  # 0 3 0, 0 1 2, 3 4 5, 6 7 or 0 3 0 0, 1 2 3, 4 5 6 7
SPACED_DIGITS = r"\d(?: \d){3,}"  # 5 5 0 7 8 1  (what the assistant itself may write)
SPOKEN_NUMBER = re.compile(rf"(?<!\d)(?:{GROUPED_DIGITS}|{SPACED_DIGITS})(?!\d)")

# speech recognition writes the digits a caller says as words ("nine seven three")
DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def digit_runs(text):
    """Every run of digits the caller said, written as digits or as words:
    'reference nine seven three, nine five one and my number is 0300 1234567' -> ['973951', '03001234567']."""
    runs, current = [], ""
    for token in re.findall(r"[a-z]+|\d", text.lower()):
        digit = token if token.isdigit() else DIGIT_WORDS.get(token)
        if digit is None:
            if current:
                runs.append(current)
            current = ""
        else:
            current += digit
    return runs + [current] if current else runs


def say_time(moment):
    """15:00 -> '3 PM', 15:30 -> '3:30 PM'."""
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    if moment.minute == 0:
        return f"{hour} {suffix}"
    return f"{hour}:{moment.minute:02d} {suffix}"


def say_day(day, today):
    """Today, tomorrow, or 'Tuesday 21 October'."""
    if day == today:
        return "today"
    if (day - today).days == 1:
        return "tomorrow"
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def say_slot(start, today):
    """'tomorrow at 3 PM'."""
    return f"{say_day(start.date(), today)} at {say_time(start)}"


def tidy_numbers(text):
    """Written form of spoken digits: '0 3 0, 0 1 2, 3 4 5, 6 7' -> '0300 1234567', '5 5 0, 7 8 1' -> '550781'.

    The voice still gets the spoken form (its pauses make digits easy to follow); this is for reading.
    """

    def join(match):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) == 11 and digits.startswith("0"):
            return f"{digits[:4]} {digits[4:]}"  # 0300 1234567
        return digits

    return SPOKEN_NUMBER.sub(join, text)


def spell_digits(digits):
    """'03001234567' -> '0 3 0, 0 1 2, 3 4 5, 6 7' (groups of three, one digit at a time)."""
    groups = [digits[i:i + 3] for i in range(0, len(digits), 3)]
    return ", ".join(" ".join(group) for group in groups)


def spell_phone(digits):
    """'03001234567' -> '0 3 0 0, 1 2 3, 4 5 6 7', the way a Pakistani mobile number is usually said (4, 3, 4).
    Any other number is read in groups of three."""
    if len(digits) == 11 and digits.startswith("0"):
        return ", ".join(" ".join(group) for group in (digits[:4], digits[4:7], digits[7:]))
    return spell_digits(digits)