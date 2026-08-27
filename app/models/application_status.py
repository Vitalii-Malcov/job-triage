from enum import StrEnum


class ApplicationStatus(StrEnum):
    NEW = "NEW"
    SAVED = "SAVED"
    APPLIED = "APPLIED"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    OFFER = "OFFER"
    WITHDRAWN = "WITHDRAWN"
