"""Minimal Schneider UMAS client (Modbus TCP function 0x5A).

Covers what the simulator needs: identify, read the PLC data dictionary (variables and
data types), read raw memory ranges, and write variables. Message layouts follow the
Apache PLC4X UMAS definitions and were verified against a Control Expert PLC simulator.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

UMAS_FC = 0x5A
OK, ERR = 0xFE, 0xFD

F_INIT_COMMS = 0x01
F_READ_ID = 0x02
F_PROJECT_INFO = 0x03
F_PLC_STATUS = 0x04
F_READ_MEMORY_BLOCK = 0x20
F_READ_VARIABLES = 0x22
F_WRITE_VARIABLES = 0x23
F_READ_DICTIONARY = 0x26

REC_VARIABLES = 0xDD02      # also used with block_no = type id for DDT member lists
REC_DATATYPES = 0xDD03


class UmasError(Exception):
    pass


class UmasConnectionError(UmasError):
    pass


@dataclass
class VarRecord:
    name: str
    type_id: int
    block: int
    offset: int


@dataclass
class TypeRecord:
    type_id: int
    name: str
    class_id: int        # 2 = structure (DDT), 4 = array, others = string etc.
    size: int


@dataclass
class MemberRecord:
    name: str
    type_id: int
    offset: int


@dataclass
class _Reader:
    data: bytes
    pos: int = 0

    def u8(self) -> int:
        v = self.data[self.pos]; self.pos += 1; return v

    def u16(self) -> int:
        v = struct.unpack_from("<H", self.data, self.pos)[0]; self.pos += 2; return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.data, self.pos)[0]; self.pos += 4; return v

    def cstr(self) -> str:
        end = self.data.index(b"\x00", self.pos)
        s = self.data[self.pos:end].decode("latin-1"); self.pos = end + 1; return s

    def rest(self) -> bytes:
        return self.data[self.pos:]


@dataclass
class PlcInfo:
    max_frame: int = 0
    hostname: str = ""
    crc: int = 0
    hardware_id: int = 0
    index: int = 0
    status_blocks: list[int] = field(default_factory=list)


class UmasClient:
    """One TCP connection; requests are matched to replies by transaction id."""

    def __init__(self, host: str, port: int = 502, unit: int = 0, timeout: float = 5.0,
                 max_in_flight: int = 4):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self.info = PlcInfo()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._tid = 0
        self._rx_task: asyncio.Task | None = None
        self._slots = asyncio.Semaphore(max_in_flight)
        self._closed_exc: Exception | None = None

    # ------------------------------------------------------------------ transport
    async def connect(self):
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise UmasConnectionError(f"cannot connect to {self.host}:{self.port}: {e}") from e
        self._closed_exc = None
        self._rx_task = asyncio.create_task(self._rx_loop())
        await self._init_comms()
        await self.read_status()
        await self._read_basic_info()

    @property
    def connected(self) -> bool:
        return self._writer is not None and self._closed_exc is None

    async def close(self):
        if self._rx_task:
            self._rx_task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._fail_pending(UmasConnectionError("connection closed"))
        self._writer = None

    def _fail_pending(self, exc: Exception):
        self._closed_exc = exc
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _rx_loop(self):
        try:
            while True:
                head = await self._reader.readexactly(7)
                tid, _proto, length, _unit = struct.unpack(">HHHB", head)
                pdu = await self._reader.readexactly(length - 1)
                fut = self._pending.pop(tid, None)
                if fut and not fut.done():
                    fut.set_result(pdu)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            self._fail_pending(UmasConnectionError(f"connection lost: {e}"))
        except asyncio.CancelledError:
            pass

    async def request(self, func: int, payload: bytes = b"") -> bytes:
        """Send a UMAS request; return the reply payload after the status byte."""
        if not self.connected:
            raise self._closed_exc or UmasConnectionError("not connected")
        async with self._slots:
            self._tid = (self._tid + 1) & 0xFFFF
            tid = self._tid
            pdu = bytes([UMAS_FC, 0x00, func]) + payload
            fut = asyncio.get_running_loop().create_future()
            self._pending[tid] = fut
            self._writer.write(struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit) + pdu)
            try:
                reply = await asyncio.wait_for(fut, self.timeout)
            except asyncio.TimeoutError:
                self._pending.pop(tid, None)
                raise UmasConnectionError(f"no reply to UMAS function 0x{func:02X}") from None
        if reply[0] & 0x80:
            raise UmasError(f"Modbus exception {reply[1] if len(reply) > 1 else '?'} for UMAS 0x{func:02X}")
        if len(reply) < 3 or reply[0] != UMAS_FC:
            raise UmasError(f"unexpected reply to UMAS 0x{func:02X}: {reply[:8].hex()}")
        if reply[2] != OK:
            raise UmasError(f"PLC rejected UMAS 0x{func:02X} (status 0x{reply[2]:02X}, {reply[3:7].hex()})")
        return reply[3:]

    # ------------------------------------------------------------------ session
    async def _init_comms(self):
        r = _Reader(await self.request(F_INIT_COMMS, b"\x00"))
        self.info.max_frame = r.u16()
        r.u16(); r.u32(); r.u32()
        n = r.u8()
        self.info.hostname = r.data[r.pos:r.pos + n].decode("latin-1", "replace")

    async def read_status(self) -> int:
        """Refresh and return the project CRC (changes when the application is rebuilt)."""
        r = _Reader(await self.request(F_PLC_STATUS))
        r.u8(); r.u16()
        n = r.u8()
        self.info.status_blocks = [r.u32() for _ in range(n)]
        if n > 4:
            self.info.crc = (self.info.status_blocks[3] + self.info.status_blocks[4]) & 0xFFFFFFFF
        return self.info.crc

    async def _read_basic_info(self):
        payload = struct.pack("<BHHHH", 0, 0x30, 0, 0, 0x21)
        r = _Reader(await self.request(F_READ_MEMORY_BLOCK, payload))
        r.u8(); r.u16()
        r.u16(); r.u16()
        self.info.index = r.u8()
        self.info.hardware_id = r.u32()

    async def project_name(self) -> str:
        """Application name; it ends the project-info reply as a length-prefixed C string."""
        data = await self.request(F_PROJECT_INFO, b"\x00")
        for p in range(len(data) - 2, -1, -1):
            if data[p] == len(data) - p - 1 and data[-1] == 0:
                return data[p + 1:-1].decode("latin-1", "replace")
        return ""

    # ------------------------------------------------------------------ dictionary
    def _dict_req(self, record_type: int, block_no: int, offset: int) -> bytes:
        p = struct.pack("<HBIHH", record_type, self.info.index, self.info.hardware_id, block_no, offset)
        if record_type == REC_VARIABLES:
            p += b"\x00\x00"
        return p

    async def read_datatypes(self) -> list[TypeRecord]:
        # paged by list index: the request's block number is the first index wanted,
        # the reply starts with the next index to ask for (0 when complete)
        out: dict[int, TypeRecord] = {}
        start = 0
        for _ in range(1000):
            r = _Reader(await self.request(F_READ_DICTIONARY, self._dict_req(REC_DATATYPES, start, 0)))
            nxt = r.u16(); r.u16()
            count = r.u16()
            for _ in range(count):
                size = r.u16(); r.u16()
                cls = r.u8(); tid = r.u8(); r.u8()
                out[tid] = TypeRecord(tid, r.cstr(), cls, size)
            if nxt == 0 or count == 0 or nxt <= start:
                break
            start = nxt
        return list(out.values())

    async def read_variables(self) -> list[VarRecord]:
        out, nxt, first = [], 0, True
        while first or nxt:
            first = False
            r = _Reader(await self.request(F_READ_DICTIONARY, self._dict_req(REC_VARIABLES, 0xFFFF, nxt)))
            r.u8(); nxt = r.u16(); r.u16()
            for _ in range(r.u16()):
                tid = r.u16(); block = r.u16(); off = r.u32(); r.u16()
                out.append(VarRecord(r.cstr(), tid, block, off))
        return out

    async def read_members(self, type_id: int) -> list[MemberRecord]:
        r = _Reader(await self.request(F_READ_DICTIONARY, self._dict_req(REC_VARIABLES, type_id, 0)))
        r.u8(); r.u32()
        out = []
        for _ in range(r.u16()):
            tid = r.u16(); off = r.u16(); r.u16(); r.u16()
            out.append(MemberRecord(r.cstr(), tid, off))
        return out

    # ------------------------------------------------------------------ data
    @property
    def chunk_size(self) -> int:
        """Largest raw read that fits in one reply frame."""
        return max(64, (self.info.max_frame or 256) - 64)

    async def read_bytes(self, block: int, address: int, length: int) -> bytes:
        ref = struct.pack("<BHBHBH", 0x11, block, 0x01, address >> 8, address & 0xFF, length)
        data = await self.request(F_READ_VARIABLES, struct.pack("<IB", self.info.crc, 1) + ref)
        if len(data) < length:
            raise UmasError(f"short read: {len(data)} of {length} bytes at {block}:{address}")
        return data[:length]

    @property
    def max_request(self) -> int:
        """Largest request payload that fits in one frame."""
        return max(64, (self.info.max_frame or 256) - 16)

    async def read_segments(self, segments: list[tuple[int, int, int]]) -> bytes:
        """Read several (block, address, length) byte ranges in one request.

        The reply is the ranges' bytes back to back, in request order.
        """
        refs = b"".join(struct.pack("<BHBHBH", 0x11, b, 0x01, a >> 8, a & 0xFF, n) for b, a, n in segments)
        data = await self.request(F_READ_VARIABLES, struct.pack("<IB", self.info.crc, len(segments)) + refs)
        want = sum(n for _, _, n in segments)
        if len(data) < want:
            raise UmasError(f"short read: {len(data)} of {want} bytes for {len(segments)} ranges")
        return data[:want]

    async def write_bytes(self, block: int, address: int, data: bytes):
        ref = struct.pack("<BHIH", 0x11, block, address, len(data)) + data
        await self.request(F_WRITE_VARIABLES, struct.pack("<IB", self.info.crc, 1) + ref)
