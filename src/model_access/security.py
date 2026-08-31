from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .contracts.enums import ErrorCode
from .errors import ModelAccessException


class FernetCredentialCipher:
    """Authenticated encryption for credential dictionaries.

    Production deployments should supply a stable key from KMS/Vault through
    MODEL_ACCESS_MASTER_KEY rather than generating one at process startup.
    """

    def __init__(self, key: str | bytes):
        self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)

    @classmethod
    def from_env(cls, name: str = "MODEL_ACCESS_MASTER_KEY") -> FernetCredentialCipher:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"{name} is required")
        return cls(value)

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, values: dict[str, Any]) -> str:
        payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self._fernet.encrypt(payload.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> dict[str, Any]:
        try:
            cleartext = self._fernet.decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise ModelAccessException(
                ErrorCode.CREDENTIAL_INVALID,
                "credential decryption failed",
            ) from exc
        value = json.loads(cleartext)
        if not isinstance(value, dict):
            raise ModelAccessException(ErrorCode.CREDENTIAL_INVALID, "invalid credential payload")
        return value


class EnvironmentSecretResolver:
    def resolve(self, reference: str) -> str:
        if not reference.startswith("env://"):
            raise ValueError(f"unsupported secret reference scheme: {reference.split('://', 1)[0]}")
        name = reference.removeprefix("env://")
        value = os.getenv(name)
        if value is None:
            raise RuntimeError(f"secret reference is not configured: env://{name}")
        return value


@dataclass(slots=True)
class URLSecurityPolicy:
    require_https_for_public: bool = True
    allow_private_networks: bool = False
    allowed_hosts: set[str] = field(default_factory=set)
    allowed_cidrs: list[str] = field(default_factory=list)
    allowed_ports: set[int] | None = None
    resolve_dns: bool = True

    def validate(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ModelAccessException(ErrorCode.REQUEST_INVALID, "base_url must use http or https")
        if parsed.username or parsed.password or parsed.fragment:
            raise ModelAccessException(
                ErrorCode.REQUEST_INVALID,
                "base_url must not include user info or fragment",
            )
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if self.allowed_ports is not None and port not in self.allowed_ports:
            raise ModelAccessException(ErrorCode.REQUEST_INVALID, "base_url port is not allowed")
        host = parsed.hostname.lower().rstrip(".")
        if host in {"metadata.google.internal", "metadata.azure.internal"}:
            raise ModelAccessException(
                ErrorCode.REQUEST_INVALID, "metadata endpoints are not allowed"
            )
        if host in self.allowed_hosts:
            return url.rstrip("/")

        addresses = self._resolve(host, port)
        public = True
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if self._in_allowed_cidr(ip):
                public = False
                continue
            if self._is_forbidden(ip):
                if not self.allow_private_networks:
                    raise ModelAccessException(
                        ErrorCode.REQUEST_INVALID,
                        "base_url resolves to a restricted network",
                    )
                public = False
        if public and self.require_https_for_public and parsed.scheme != "https":
            raise ModelAccessException(ErrorCode.REQUEST_INVALID, "public base_url must use https")
        return url.rstrip("/")

    def _resolve(self, host: str, port: int) -> set[str]:
        try:
            ipaddress.ip_address(host)
            return {host}
        except ValueError:
            pass
        if not self.resolve_dns:
            return set()
        try:
            return {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ModelAccessException(
                ErrorCode.REQUEST_INVALID,
                "base_url host cannot be resolved",
            ) from exc

    def _in_allowed_cidr(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(ip in ipaddress.ip_network(cidr) for cidr in self.allowed_cidrs)

    @staticmethod
    def _is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )


def mask_secret(value: str) -> str:
    if not value:
        return "****"
    prefix_length = min(3, max(0, len(value) - 4))
    suffix_length = min(4, len(value))
    return f"{value[:prefix_length]}****{value[-suffix_length:]}"
