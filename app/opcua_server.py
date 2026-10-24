"""OPC UA server exposing the loaded program's tags (asyncua)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from asyncua import Server, ua
from asyncua.server.internal_session import InternalSession

from .iec_values import coerce
from .xef_parser import Program, Tag

log = logging.getLogger(__name__)

NAMESPACE_URI = "urn:m580-opcua-simulator:tags"


# asyncua doesn't keep what a client said about itself at session creation; record it
# (application and session name, time) for the diagnostics page.
_orig_create_session = InternalSession.create_session


async def _create_session(self, params, sockname=None):
    desc = getattr(params, "ClientDescription", None)
    self.client_app = desc.ApplicationName.Text if desc and desc.ApplicationName else ""
    self.client_session_name = getattr(params, "SessionName", "") or ""
    self.created_at = time.time()
    return await _orig_create_session(self, params, sockname=sockname)


InternalSession.create_session = _create_session


class WriteRejected(Exception):
    """Raised by a write hook; carries the OPC UA status code to return to the client."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status


class OpcUaServer:
    """Owns the asyncua server and keeps one variable node per tag.

    Client writes are validated here (access, type, range) before asyncua stores
    them. If `write_hook` is set (PLC mode) the value is first written to the PLC and
    the value it returns is stored. Accepted writes are reported through `on_client_write`.
    """

    def __init__(self, host: str, port: int, server_name: str,
                 on_client_write: Callable[[str, object], None]):
        self.endpoint = f"opc.tcp://{host}:{port}/"
        self.port = port
        self.server_name = server_name
        self.on_client_write = on_client_write
        self.write_hook: Callable[[str, object], Awaitable[object]] | None = None
        self.server = Server()
        self.ns = 2
        self._root: ua.NodeId | None = None
        self._root_name: str | None = None
        self._tags: dict[str, Tag] = {}            # NodeId string id -> Tag
        self._folders: dict[str, str] = {}          # structure folder path -> comment, parents first
        self._meta: dict[str, tuple] = {}           # tag path -> (comment, writable) on the node
        self._reads: dict[str, float] = {}          # tag path -> last time a client read it
        self._orig_read = None
        self._orig_write = None

    async def start(self):
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name(self.server_name)
        await self.server.set_application_uri("urn:m580-opcua-simulator")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await self.server.register_namespace(NAMESPACE_URI)

        attr_service = self.server.iserver.attribute_service
        self._orig_write = attr_service.write
        attr_service.write = self._checked_write
        self._orig_read = attr_service.read
        attr_service.read = self._tracked_read
        await self.server.start()
        log.info("OPC UA server listening on %s", self.endpoint)

    async def stop(self):
        await self.server.stop()

    @property
    def session_count(self) -> int:
        return InternalSession._current_connections

    def node_id(self, path: str) -> ua.NodeId:
        return ua.NodeId(path, self.ns, ua.NodeIdType.String)

    # ---------------------------------------------------------------- address space
    def _folder_id(self, prefix: str) -> ua.NodeId:
        return ua.NodeId(prefix, self.ns, ua.NodeIdType.String)

    def _parent_id(self, path: str) -> ua.NodeId:
        prefixes = _prefixes(path)
        return self._folder_id(prefixes[-1]) if prefixes else self._root

    async def _retire(self, nids: list[ua.NodeId]):
        """Tell monitored items their node is going away (BadNodeIdUnknown).

        asyncua calls the async subscription callback without awaiting it, so clients
        would never hear about it; notify them here and drop the callbacks first.
        """
        aspace = self.server.iserver.aspace
        gone = ua.DataValue(StatusCode_=ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown))
        for nid in nids:
            if nid not in aspace:
                continue
            attr = aspace[nid].attributes.get(ua.AttributeIds.Value)
            if attr is None:
                continue
            for handle, callback in list(attr.datachange_callbacks.items()):
                try:
                    result = callback(handle, gone)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("Notifying a monitored item of node removal failed")
                aspace.delete_datachange_callback(handle)

    async def _delete(self, nids: list[ua.NodeId], unlink: dict | None = None):
        """Delete nodes without asyncua's whole-address-space reference scan.

        asyncua scans every node once per deleted node (minutes for a large project).
        Only our tree refers to our nodes, so references are removed from the surviving
        parents given in `unlink` ({parent id: {child ids}}) instead.
        """
        await self._retire(nids)
        session = self.server.iserver.isession
        for i in range(0, len(nids), 500):
            items = []
            for nid in nids[i:i + 500]:
                item = ua.DeleteNodesItem()
                item.NodeId, item.DeleteTargetReferences = nid, False
                items.append(item)
            await session.delete_nodes(ua.DeleteNodesParameters(NodesToDelete=items))
            await asyncio.sleep(0)
        aspace = self.server.iserver.aspace
        for parent, children in (unlink or {}).items():
            if parent in aspace:
                node = aspace[parent]
                node.references = [r for r in node.references if r.NodeId not in children]

    async def clear(self):
        """Remove the whole project tree."""
        if self._root is None:
            return
        t = time.time()
        ids = [self.node_id(p) for p in self._tags] + [self._folder_id(p) for p in reversed(self._folders)]
        await self._delete(ids)
        await self._retire([self._root])
        root = ua.DeleteNodesItem()
        root.NodeId, root.DeleteTargetReferences = self._root, True
        await self.server.iserver.isession.delete_nodes(ua.DeleteNodesParameters(NodesToDelete=[root]))
        log.info("Address space cleared: %d nodes in %.1fs", len(ids) + 1, time.time() - t)
        self._root, self._root_name, self._tags, self._folders, self._meta = None, None, {}, {}, {}

    async def load(self, program: Program, values: dict[str, object]):
        """Make the address space match `program`.

        Nodes for tags that already exist (same path and data type) are kept and updated
        in place, so clients' subscriptions keep working; only removed tags are deleted
        and only new ones are created.
        """
        t = time.time()
        if self._root is not None and self._root_name != program.name:
            await self.clear()
        aspace = self.server.iserver.aspace
        if self._root is None:
            root_id = ua.NodeId(program.name, self.ns, ua.NodeIdType.String)
            root = await self.server.nodes.objects.add_folder(root_id, ua.QualifiedName(program.name, self.ns))
            self._root, self._root_name = root.nodeid, program.name
        await aspace.write_attribute_value(self._root, ua.AttributeIds.Description, ua.DataValue(ua.Variant(
            ua.LocalizedText(f"{program.name} {program.version} ({program.plc})"), ua.VariantType.LocalizedText)))

        # the wanted tree; folders in parent-first order
        want_folders: dict[str, tuple[str, ua.NodeId]] = {}
        want_tags: dict[str, tuple[Tag, ua.NodeId, str]] = {}
        for tag in program.tags:
            parts = _split_path(tag.path)
            parent, prefix = self._root, ""
            for part in parts[:-1]:
                prefix = prefix + ("" if part.startswith("[") or not prefix else ".") + part
                want_folders.setdefault(prefix, (part, parent))
                parent = self._folder_id(prefix)
            want_tags[tag.path] = (tag, parent, parts[-1])

        # remove tags that are gone or changed data type, and folders that are gone
        gone_tags = [p for p, old in self._tags.items()
                     if p not in want_tags or old.ti.ua_type != want_tags[p][0].ti.ua_type]
        gone_folders = [p for p in self._folders if p not in want_folders]
        if gone_tags or gone_folders:
            unlink: dict[ua.NodeId, set] = {}
            for p in gone_tags:
                unlink.setdefault(self._parent_id(p), set()).add(self.node_id(p))
            for p in gone_folders:
                unlink.setdefault(self._parent_id(p), set()).add(self._folder_id(p))
            await self._delete([self.node_id(p) for p in gone_tags] +
                               [self._folder_id(p) for p in reversed(gone_folders)], unlink)
            for p in gone_tags:
                del self._tags[p]
                self._meta.pop(p, None)
            for p in gone_folders:
                del self._folders[p]
        kept = [p for p in want_tags if p in self._tags]

        # create new folders and tags
        items: list[ua.AddNodesItem] = []
        for prefix, (name, parent) in want_folders.items():
            if prefix in self._folders:
                continue
            comment = program.struct_comments.get(prefix, "")
            # attributes must be assigned (not passed to __init__) so that
            # asyncua records them in SpecifiedAttributes
            attrs = ua.ObjectAttributes()
            attrs.DisplayName = ua.LocalizedText(name)
            attrs.Description = ua.LocalizedText(comment)
            attrs.EventNotifier = 0
            attrs.WriteMask = attrs.UserWriteMask = 0
            items.append(_add_item(self._folder_id(prefix), name, self.ns, ua.NodeClass.Object, parent,
                                   ua.ObjectIds.Organizes, ua.ObjectIds.FolderType, attrs))
            self._folders[prefix] = comment
        for path, (tag, parent, name) in want_tags.items():
            if path in self._tags:
                continue
            vtype = getattr(ua.VariantType, tag.ti.ua_type)
            attrs = ua.VariableAttributes()
            attrs.DisplayName = ua.LocalizedText(name)
            attrs.Description = ua.LocalizedText(tag.comment)
            attrs.Value = ua.Variant(values[path], vtype)
            attrs.DataType = ua.NodeId(vtype.value)
            attrs.ValueRank = ua.ValueRank.Scalar
            attrs.ArrayDimensions = None
            attrs.WriteMask = attrs.UserWriteMask = 0
            attrs.Historizing = False
            attrs.AccessLevel = attrs.UserAccessLevel = _access(tag)
            ref = ua.ObjectIds.HasComponent if parent != self._root else ua.ObjectIds.Organizes
            items.append(_add_item(self.node_id(path), name, self.ns, ua.NodeClass.Variable,
                                   parent, ref, ua.ObjectIds.BaseDataVariableType, attrs))
            self._meta[path] = (tag.comment, tag.writable)
        # add in chunks, yielding so the web UI and other OPC UA sessions stay responsive
        results = []
        for i in range(0, len(items), 250):
            results += await self.server.iserver.isession.add_nodes(items[i:i + 250])
            await asyncio.sleep(0)
        bad = [r for r in results if not r.StatusCode.is_good()]
        if bad:
            log.warning("%d nodes failed to add, first: %s", len(bad), bad[0].StatusCode)
        self._tags = {p: want_tags[p][0] for p in want_tags}

        # existing nodes: new comment/access and value
        await self._sync_meta([want_tags[p][0] for p in kept], program.struct_comments)
        for i, path in enumerate(kept):
            await self.publish(path, values[path])
            if i % 500 == 499:
                await asyncio.sleep(0)
        log.info("Address space loaded: %d tags (%d kept, %d added, %d removed), %d folders in %.1fs",
                 len(self._tags), len(kept), len(want_tags) - len(kept), len(gone_tags),
                 len(self._folders), time.time() - t)

    async def publish(self, path: str, value, status: int = ua.StatusCodes.Good, ts: datetime | None = None):
        """Push a value from the web UI or the PLC (bypasses client access checks)."""
        tag = self._tags[path]
        vtype = getattr(ua.VariantType, tag.ti.ua_type)
        now = datetime.now(timezone.utc)
        await self.server.write_attribute_value(
            self.node_id(path),
            ua.DataValue(ua.Variant(value, vtype), StatusCode_=ua.StatusCode(status),
                         SourceTimestamp=ts or now, ServerTimestamp=now))

    async def mark_all(self, status: int):
        """Give every tag a status (e.g. BadNotConnected), keeping its last value."""
        aspace = self.server.iserver.aspace
        now = datetime.now(timezone.utc)
        for i, path in enumerate(list(self._tags)):
            nid = self.node_id(path)
            current = aspace.read_attribute_value(nid, ua.AttributeIds.Value)
            await self.server.write_attribute_value(
                nid, ua.DataValue(current.Value, StatusCode_=ua.StatusCode(status),
                                  SourceTimestamp=current.SourceTimestamp, ServerTimestamp=now))
            if i % 500 == 499:
                await asyncio.sleep(0)

    async def update_metadata(self, tags: list[Tag], struct_comments: dict[str, str]):
        """Refresh Description and access level on existing nodes (keeps subscriptions)."""
        tags = [t for t in tags if t.path in self._tags]
        for t in tags:
            self._tags[t.path] = t
        await self._sync_meta(tags, struct_comments)

    async def _sync_meta(self, tags: list[Tag], struct_comments: dict[str, str]):
        aspace = self.server.iserver.aspace
        for i, tag in enumerate(tags):
            if self._meta.get(tag.path) == (tag.comment, tag.writable):
                continue
            nid = self.node_id(tag.path)
            access = _access(tag)
            for attr, variant in ((ua.AttributeIds.Description,
                                   ua.Variant(ua.LocalizedText(tag.comment), ua.VariantType.LocalizedText)),
                                  (ua.AttributeIds.AccessLevel, ua.Variant(access, ua.VariantType.Byte)),
                                  (ua.AttributeIds.UserAccessLevel, ua.Variant(access, ua.VariantType.Byte))):
                await aspace.write_attribute_value(nid, attr, ua.DataValue(variant))
            self._meta[tag.path] = (tag.comment, tag.writable)
            if i % 500 == 499:
                await asyncio.sleep(0)
        for prefix, old in self._folders.items():
            comment = struct_comments.get(prefix, "")
            if comment != old:
                await aspace.write_attribute_value(
                    self._folder_id(prefix), ua.AttributeIds.Description,
                    ua.DataValue(ua.Variant(ua.LocalizedText(comment), ua.VariantType.LocalizedText)))
                self._folders[prefix] = comment

    # ---------------------------------------------------------------- what clients watch
    def _tracked_read(self, params: ua.ReadParameters):
        """Note which tags clients read (they are polled faster for a while)."""
        now = time.time()
        for rv in params.NodesToRead:
            nid = rv.NodeId
            if (nid.NamespaceIndex == self.ns and rv.AttributeId == ua.AttributeIds.Value
                    and nid.Identifier in self._tags):
                self._reads[nid.Identifier] = now
        return self._orig_read(params)

    def subscribed_paths(self) -> set[str]:
        """Tags with an active subscription (monitored item) from any client.

        Every monitored item registers a data-change callback; asyncua keeps them in
        handle -> (node id, attribute), so this costs O(subscribed items), not O(tags).
        """
        out = set()
        for nid, attr in list(self.server.iserver.aspace._handle_to_attribute_map.values()):
            if (attr == ua.AttributeIds.Value and nid.NamespaceIndex == self.ns
                    and nid.Identifier in self._tags):
                out.add(nid.Identifier)
        return out

    def clients(self) -> list[dict]:
        """Connected OPC UA clients with their subscriptions, for the diagnostics page."""
        subs = list(self.server.iserver.subscription_service.subscriptions.values())
        out = []
        for proto in list(getattr(self.server.bserver, "clients", [])):
            processor = getattr(proto, "processor", None)
            session = getattr(processor, "session", None)
            peer = proto.peer_name or ("?", 0)
            mine = [x for x in subs if session is not None and x.session_id == session.session_id]
            out.append({
                "address": f"{peer[0]}:{peer[1]}",
                "application": getattr(session, "client_app", "") if session else "",
                "session": getattr(session, "client_session_name", "") if session else "",
                "state": session.state.name if session else "no session",
                "since": getattr(session, "created_at", None) if session else None,
                "subscriptions": len(mine),
                "monitored_items": sum(len(x.monitored_item_srv._monitored_items) for x in mine),
                "publishing_ms": sorted({round(x.data.RevisedPublishingInterval) for x in mine}),
            })
        return out

    def subscriber_count(self, path: str) -> int:
        """Number of monitored items (across all clients) on one tag."""
        try:
            node = self.server.iserver.aspace[self.node_id(path)]
            return len(node.attributes[ua.AttributeIds.Value].datachange_callbacks)
        except KeyError:
            return 0

    def read_paths(self, read_ttl: float = 60.0) -> set[str]:
        """Tags a client read in the last `read_ttl` seconds."""
        now = time.time()
        for path, t in list(self._reads.items()):
            if now - t > read_ttl or path not in self._tags:
                del self._reads[path]
        return set(self._reads)

    # ---------------------------------------------------------------- client writes
    async def _checked_write(self, params: ua.WriteParameters, user=None):
        """Validate client writes to tag nodes; pass everything else through."""
        results: list[ua.StatusCode | None] = []
        passthrough = []
        accepted: list[tuple[str, object]] = []
        for wv in params.NodesToWrite:
            nid = wv.NodeId
            tag = self._tags.get(nid.Identifier) if nid.NamespaceIndex == self.ns else None
            if tag is None or wv.AttributeId != ua.AttributeIds.Value:
                results.append(None)
                passthrough.append(wv)
                continue
            if not tag.writable:
                results.append(ua.StatusCode(ua.StatusCodes.BadNotWritable))
                continue
            variant = wv.Value.Value if wv.Value is not None else None
            if variant is None or variant.Value is None:
                results.append(ua.StatusCode(ua.StatusCodes.BadTypeMismatch))
                continue
            try:
                value = _coerce_client_value(variant.Value, tag)
            except TypeError:
                results.append(ua.StatusCode(ua.StatusCodes.BadTypeMismatch))
                continue
            except ValueError:
                results.append(ua.StatusCode(ua.StatusCodes.BadOutOfRange))
                continue
            if self.write_hook is not None:
                try:
                    value = await self.write_hook(tag.path, value)
                except WriteRejected as e:
                    log.info("Client write to %s rejected: %s", tag.path, e)
                    results.append(ua.StatusCode(e.status))
                    continue
            vtype = getattr(ua.VariantType, tag.ti.ua_type)
            dv = ua.DataValue(ua.Variant(value, vtype),
                              StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                              SourceTimestamp=wv.Value.SourceTimestamp or datetime.now(timezone.utc),
                              ServerTimestamp=datetime.now(timezone.utc))
            status = await self.server.iserver.aspace.write_attribute_value(nid, ua.AttributeIds.Value, dv)
            results.append(status)
            if status.is_good():
                accepted.append((tag.path, value))

        if passthrough:
            sub = ua.WriteParameters(NodesToWrite=passthrough)
            it = iter(await (self._orig_write(sub, user=user) if user is not None
                             else self._orig_write(sub)))
            results = [r if r is not None else next(it) for r in results]

        for path, value in accepted:
            self.on_client_write(path, value)
        return results


def _coerce_client_value(raw, tag: Tag):
    kind = tag.ti.kind
    if kind == "bool":
        if not isinstance(raw, bool):
            raise TypeError
    elif kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError
        if isinstance(raw, float) and not raw.is_integer():
            raise TypeError
        raw = int(raw)
    elif kind == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError
    elif not isinstance(raw, str):
        raise TypeError
    return coerce(raw, tag.ti)


def _access(tag: Tag) -> int:
    return ua.AccessLevel.CurrentRead.mask | (ua.AccessLevel.CurrentWrite.mask if tag.writable else 0)


def _add_item(nid, name, ns, node_class, parent, ref_type, type_def, attrs) -> ua.AddNodesItem:
    item = ua.AddNodesItem()
    item.RequestedNewNodeId = nid
    item.BrowseName = ua.QualifiedName(name, ns)
    item.NodeClass = node_class
    item.ParentNodeId = parent
    item.ReferenceTypeId = ua.NodeId(ref_type)
    item.TypeDefinition = ua.NodeId(type_def)
    item.NodeAttributes = attrs
    return item


def _prefixes(path: str) -> list[str]:
    """Folder node ids above a tag: 'A.B[1].C' -> ['A', 'A.B', 'A.B[1]']"""
    out, prefix = [], ""
    for part in _split_path(path)[:-1]:
        prefix = prefix + ("" if part.startswith("[") or not prefix else ".") + part
        out.append(prefix)
    return out


def _split_path(path: str) -> list[str]:
    """'A.B[1].C' -> ['A', 'B', '[1]', 'C']"""
    out = []
    for seg in path.split("."):
        i = seg.find("[")
        if i > 0:
            out.append(seg[:i])
            seg = seg[i:]
        while seg.startswith("["):
            j = seg.index("]") + 1
            out.append(seg[:j])
            seg = seg[j:]
        if seg:
            out.append(seg)
    return out
