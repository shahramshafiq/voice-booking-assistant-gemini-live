"""The shape of the input for each tool. Pydantic checks every call before any code runs.

The same classes also produce the JSON Schema that is shown to the model (see tools.py),
so the model's instructions and the real validation can never disagree.
"""
from typing import Optional

from pydantic import BaseModel, Field


class NoArgs(BaseModel):
    pass


class CheckAvailabilityArgs(BaseModel):
    date: str = Field(description="The day to check: YYYY-MM-DD, or words like today, tomorrow, friday, 21 October.")
    duration: int = Field(ge=5, le=480, description="Appointment length in minutes.")
    preferred_time: Optional[str] = Field(
        default=None,
        description="The caller's own words about the time, e.g. afternoon, after 3 PM, around 2 PM. Leave out if none.",
    )


class DateRange(BaseModel):
    start_date: str = Field(description="First day: YYYY-MM-DD or words like tomorrow, friday, next week.")
    end_date: Optional[str] = Field(default=None, description="Last day. Leave out to search only the first day.")


class FindSlotsArgs(BaseModel):
    service_id: str = Field(description="The service id, for example consultation.")
    date_range: DateRange
    time_preference: Optional[str] = Field(
        default=None,
        description="The caller's own words about the time, e.g. morning, after 3 PM. Leave out if none.",
    )


class CustomerDetails(BaseModel):
    name: str = Field(min_length=1, max_length=60, description="The customer's name.")
    phone: str = Field(description="The customer's phone number, digits only.")
    notes: Optional[str] = Field(default=None, max_length=200, description="Anything the customer wants noted. Optional.")


class CreateBookingArgs(BaseModel):
    customer_details: CustomerDetails
    service_id: str
    start_time: str = Field(description="Copied exactly from a slot returned by find_available_slots.")
    end_time: str = Field(description="Copied exactly from the same slot.")
    skip_confirmation: bool = Field(
        default=False,
        description="true ONLY when the caller has clearly told you to go ahead without asking them to confirm, for "
                    "example 'just book it, don't ask me again'. Otherwise leave it out.",
    )


class BookingLookupArgs(BaseModel):
    booking_reference: str = Field(description="The booking reference, digits only.")
    customer_phone: str = Field(description="The phone number the booking was made with, digits only.")


class RescheduleArgs(BookingLookupArgs):
    new_date: str = Field(description="The new day: YYYY-MM-DD or words like tomorrow, friday.")
    new_time: str = Field(description="The new start time, for example 3 PM or 15:00.")
    skip_confirmation: bool = Field(
        default=False,
        description="true ONLY when the caller has clearly told you to go ahead without asking them to confirm, for "
                    "example 'just book it, don't ask me again'. Otherwise leave it out.",
    )


class CancelArgs(BookingLookupArgs):
    skip_confirmation: bool = Field(
        default=False,
        description="true ONLY when the caller has clearly told you to go ahead without asking them to confirm, for "
                    "example 'just book it, don't ask me again'. Otherwise leave it out.",
    )
