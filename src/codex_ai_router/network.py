from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class NetworkMode(str, Enum):
    AUTO = "AUTO"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


@dataclass
class NetworkState:
    mode: NetworkMode = NetworkMode.AUTO
    remote_state: str = "UNKNOWN"

    @property
    def remote_allowed(self) -> bool:
        return self.mode is not NetworkMode.OFFLINE

    def probe(self, endpoint: str, timeout: float = 2.0) -> str:
        """Probe one concrete endpoint; Router never infers VPN state."""
        if not self.remote_allowed:
            self.remote_state = "OFFLINE"
            return self.remote_state
        parsed = urlparse(endpoint)
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            self.remote_state = "LOCAL"
            return self.remote_state
        try:
            request = Request(endpoint, method="HEAD")
            with urlopen(request, timeout=timeout):
                self.remote_state = "ONLINE"
        except HTTPError:
            # A reachable endpoint can reject HEAD/auth while still being online.
            self.remote_state = "ONLINE"
        except (URLError, TimeoutError, OSError):
            self.remote_state = "OFFLINE_OR_REMOTE_UNAVAILABLE"
        return self.remote_state
