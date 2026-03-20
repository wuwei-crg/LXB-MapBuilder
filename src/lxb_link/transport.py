"""
LXB-Link Transport Layer

This module implements a reliable TCP transport with Stop-and-Wait semantics
at command level (seq + ACK matching) for deterministic request/response flow.
"""

import logging
import socket
import struct
import time
from typing import Optional

from .constants import (
    CMD_ACK,
    CMD_SCREENSHOT,
    CRC_SIZE,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    SOCKET_BUFFER_SIZE,
    VERSION_V1,
    VERSION_V2,
    LXBChecksumError,
    LXBProtocolError,
    LXBTimeoutError,
)
from .protocol import ProtocolFrame


logger = logging.getLogger(__name__)


class Transport:
    """
    Reliable TCP transport with command-level Stop-and-Wait semantics.
    """

    def __init__(
        self,
        remote_host: str,
        remote_port: int,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ):
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.timeout = timeout
        self.max_retries = max_retries

        self._seq = 0
        self._sock: Optional[socket.socket] = None
        self._connected = False

        logger.info(
            f"Transport initialized (TCP): {remote_host}:{remote_port}, "
            f"timeout={timeout}s, max_retries={max_retries}"
        )

    def connect(self) -> None:
        if self._connected:
            logger.warning("Transport already connected")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_SIZE)
        sock.connect((self.remote_host, self.remote_port))

        self._sock = sock
        self._connected = True
        logger.info(f"Connected to {self.remote_host}:{self.remote_port} (TCP)")

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._connected = False
                logger.info("Transport disconnected")

    def reset_runtime_state(
        self,
        reset_seq: bool = True,
        drain_timeout: float = 0.01,
        max_frames: int = 1024,
    ) -> int:
        if reset_seq:
            self._seq = 0
        # TCP stream has no datagram receive queue semantics like UDP drain.
        return 0

    def _next_seq(self) -> int:
        current_seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return current_seq

    def _send_frame(self, frame: bytes) -> None:
        if not self._connected or not self._sock:
            raise LXBProtocolError("Transport not connected", 0)
        self._sock.sendall(frame)
        logger.debug(f"Sent frame: {len(frame)} bytes")

    def _apply_socket_timeout(self) -> None:
        """
        Synchronize socket timeout with current transport timeout.
        """
        if not self._connected or not self._sock:
            raise LXBProtocolError("Transport not connected", 0)
        self._sock.settimeout(self.timeout)

    def _recv_exact(self, size: int) -> bytes:
        if not self._connected or not self._sock:
            raise LXBProtocolError("Transport not connected", 0)

        chunks = []
        remaining = size
        while remaining > 0:
            part = self._sock.recv(remaining)
            if not part:
                raise LXBProtocolError(
                    "Connection closed while receiving frame", 0
                )
            chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)

    def _recv_frame(self) -> bytes:
        # Read magic(2)+version(1) first, then version-specific header.
        prefix = self._recv_exact(3)
        version = prefix[2]

        if version == VERSION_V2:
            header_size = ProtocolFrame.HEADER_SIZE_V2
        elif version == VERSION_V1:
            header_size = ProtocolFrame.HEADER_SIZE_V1
        else:
            raise LXBProtocolError(f"Unsupported frame version: 0x{version:02X}", 0)

        header_rest = self._recv_exact(header_size - 3)
        header = prefix + header_rest

        if version == VERSION_V2:
            payload_len = struct.unpack(">I", header[8:12])[0]
        else:
            payload_len = struct.unpack(">H", header[8:10])[0]

        body = self._recv_exact(payload_len + CRC_SIZE)
        frame = header + body
        logger.debug(f"Received frame: {len(frame)} bytes")
        return frame

    def send_reliable(self, cmd: int, payload: bytes = b"") -> bytes:
        seq = self._next_seq()
        frame = ProtocolFrame.pack(seq, cmd, payload)
        retry_count = 0
        last_error: Optional[Exception] = None

        logger.info(
            f"Sending reliable frame: seq={seq}, cmd=0x{cmd:02X}, "
            f"payload={len(payload)} bytes"
        )

        while retry_count <= self.max_retries:
            try:
                # timeout can be adjusted dynamically by caller (e.g. timeout_factor).
                self._apply_socket_timeout()
                self._send_frame(frame)
                send_time = time.time()

                while True:
                    try:
                        recv_data = self._recv_frame()
                        recv_seq, recv_cmd, recv_payload = ProtocolFrame.unpack(recv_data)

                        if recv_cmd == CMD_ACK:
                            if recv_seq == seq:
                                elapsed = time.time() - send_time
                                logger.info(
                                    f"ACK received: seq={seq}, retry={retry_count}, "
                                    f"rtt={elapsed*1000:.1f}ms"
                                )
                                return recv_payload
                            logger.warning(
                                f"ACK sequence mismatch: expected {seq}, received {recv_seq}"
                            )
                            continue

                        logger.warning(
                            f"Unexpected command: 0x{recv_cmd:02X} "
                            f"(expected ACK=0x{CMD_ACK:02X})"
                        )
                        continue

                    except (LXBProtocolError, LXBChecksumError) as e:
                        logger.warning(f"Invalid frame received: {e}")
                        continue

            except socket.timeout:
                logger.warning(
                    f"Timeout waiting for ACK: seq={seq}, "
                    f"retry={retry_count}/{self.max_retries}"
                )
            except Exception as e:
                last_error = e
                logger.error(f"Error in send_reliable: {e}")

            retry_count += 1
            if retry_count <= self.max_retries:
                logger.info(f"Retrying transmission: attempt {retry_count}")

        if last_error:
            raise last_error
        raise LXBTimeoutError(
            f"Maximum retries ({self.max_retries}) exceeded for "
            f"seq={seq}, cmd=0x{cmd:02X}"
        )

    def send_and_forget(self, cmd: int, payload: bytes = b"") -> None:
        seq = self._next_seq()
        frame = ProtocolFrame.pack(seq, cmd, payload)
        self._send_frame(frame)
        logger.info(f"Sent unreliable frame: seq={seq}, cmd=0x{cmd:02X}")

    def request_screenshot_fragmented(self) -> bytes:
        """
        Backward-compatible method name.

        The transport is now TCP-based, so screenshot uses single command ACK
        payload: status[1] + image_bytes.
        """
        ack_payload = self.send_reliable(CMD_SCREENSHOT, b"")
        if not ack_payload:
            raise LXBProtocolError("Empty screenshot response payload", 0)
        status = ack_payload[0]
        if status != 0x01:
            raise LXBProtocolError(f"Screenshot failed with status=0x{status:02X}", 0)
        return ack_payload[1:]

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    def __del__(self):
        self.disconnect()
