from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Union

@dataclass
class Session:
    id: Optional[Union[int, str]] # Updated to handle both SQL and Firestore IDs
    name: str
    email: str
    source_ip: str
    code: str
    start: str
    expiry: str
    active: int
    status: str
    ask_email: int = 0
    ask_phone: int = 0

    @property
    def is_expired(self) -> bool:
        """Determines if a session has naturally expired or was intentionally ended."""
        return datetime.now() > datetime.fromisoformat(self.expiry) or self.status == 'ended'

@dataclass
class Entry:
    id: Optional[Union[int, str]]
    session_id: Union[int, str]
    timestamp: str
    subject_name: str
    email: str
    phone: str
    ip_address: str
    device_id: str
