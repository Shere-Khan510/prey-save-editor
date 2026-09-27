"""
preysave - read/write Prey (2017, Arkane) save files.

Container (save.CSF / *.level):
    "CRY3SDK" + RC4(key = 256 zero bytes, first 1024 keystream bytes dropped)(payload)
payload:
    zlib chunks: [u32 compressed_size][u32 uncompressed_size][zlib stream] ...
    64-byte XMLCPB file header (uncompressed), magic "0XBP"
    header[4:20] = MD5(chunks + header with bytes 4..20 zeroed)
decompressed data (CryEngine XMLCPB binary XML):
    [nodes][tags table][attr-name table][string-data table][attr-set table]
"""
import hashlib
import struct
import zlib

MAGIC = b"CRY3SDK"
XMLCPB_MAGIC = 0x50425830  # "0XBP"
CHUNK_SIZE = 0x8000

# ---------------------------------------------------------------- crypto

_KEYSTREAM = bytearray()


def _keystream(n):
    """RC4 keystream with an all-zero 256-byte key, 1024 bytes dropped (cached)."""
    global _KEYSTREAM
    if len(_KEYSTREAM) < n:
        n2 = max(n, 2 * len(_KEYSTREAM), 1 << 20)
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i]) & 255
            s[i], s[j] = s[j], s[i]
        out = bytearray(n2 + 1024)
        i = j = 0
        for k in range(n2 + 1024):
            i = (i + 1) & 255
            si = s[i]
            j = (j + si) & 255
            sj = s[j]
            s[i] = sj
            s[j] = si
            out[k] = s[(si + sj) & 255]
        _KEYSTREAM = out[1024:]
    return bytes(_KEYSTREAM[:n])


def _xor(data):
    ks = _keystream(len(data))
    return (int.from_bytes(data, "little") ^ int.from_bytes(ks, "little")).to_bytes(len(data), "little")


def decrypt(raw):
    if raw[:7] != MAGIC:
        raise ValueError("not a Prey save (missing CRY3SDK signature)")
    return _xor(raw[7:])


def encrypt(payload):
    return MAGIC + _xor(payload)


# ---------------------------------------------------------------- container


def unpack_container(raw):
    """-> (decompressed XMLCPB data, header u32 list[16]). Verifies MD5."""
    p = decrypt(raw)
    if len(p) < 64:
        raise ValueError("file too small")
    header = p[-64:]
    fields = list(struct.unpack("<16I", header))
    if fields[0] != XMLCPB_MAGIC:
        raise ValueError("bad XMLCPB signature - file corrupt or wrong format")
    zeroed = header[:4] + bytes(16) + header[20:]
    if hashlib.md5(p[:-64] + zeroed).digest() != header[4:20]:
        raise ValueError("MD5 check failed - file corrupt")
    out = bytearray()
    pos = 0
    body = p[:-64]
    while pos < len(body):
        c, u = struct.unpack_from("<II", body, pos)
        d = zlib.decompress(body[pos + 8:pos + 8 + c])
        if len(d) != u:
            raise ValueError("chunk size mismatch")
        out += d
        pos += 8 + c
    return bytes(out), fields


def pack_container(data, fields):
    body = bytearray()
    for i in range(0, len(data), CHUNK_SIZE):
        blk = data[i:i + CHUNK_SIZE]
        c = zlib.compress(blk, 5)
        body += struct.pack("<II", len(c), len(blk)) + c
    f = list(fields)
    f[0] = XMLCPB_MAGIC
    f[1:5] = [0, 0, 0, 0]
    header = struct.pack("<16I", *f)
    md5 = hashlib.md5(bytes(body) + header).digest()
    header = header[:4] + md5 + header[20:]
    return encrypt(bytes(body) + header)


# ---------------------------------------------------------------- attribute types

T_STR, T_INT32, T_F1, T_F3, T_F4, T_INT64, T_RAW = 0, 1, 2, 3, 4, 5, 6
T_POS16, T_NEG16, T_POS8, T_NEG8, T_F1_1DEC = 7, 8, 9, 10, 11
T_F3_1C, T_F3_2C, T_F3_3C, T_F4_1C, T_F4_2C, T_F4_3C, T_F4_4C = 12, 13, 14, 15, 16, 17, 18
T_0, T_10, T_255 = 19, 29, 30
T_F3_100, T_F3_010, T_F3_001 = 31, 32, 33
T_CONSTSTR = 34  # 34..63

TYPE_NAMES = {0: "STR", 1: "INT32", 2: "F1", 3: "F3", 4: "F4", 5: "INT64", 6: "RAWDATA",
              7: "POS16", 8: "NEG16", 9: "POS8", 10: "NEG8", 11: "F1_1DEC",
              12: "F3_1CONST", 13: "F3_2CONST", 14: "F3_3CONST",
              15: "F4_1CONST", 16: "F4_2CONST", 17: "F4_3CONST", 18: "F4_4CONST",
              30: "255", 31: "F3_100", 32: "F3_010", 33: "F3_001"}
for _i in range(11):
    TYPE_NAMES[19 + _i] = str(_i)

CONST_STRINGS = ["n", "table", "RigidBodyEx", "bRigidBodyActive", "false", "true", "Idle", "bActive",
                 "Light", "h", "s", "vec", "ParticleEffect", "SmartObject", "b", "TagPoint", "GrabableLedge",
                 "health", "nParticleSlot", "zero", "enabled", "state", "Alive", "currentSlot", "LastHit",
                 "pos", "impulse", "shooterId", "bIgnoreObstruction", "bIgnoreCulling"]
_CONST_STR_IDX = {s: i for i, s in enumerate(CONST_STRINGS)}

_FIXED_SIZE = {0: 2, 1: 4, 2: 4, 3: 12, 4: 16, 5: 8, 7: 2, 8: 2, 9: 1, 10: 1, 11: 1,
               12: 9, 13: 5, 14: 1, 15: 13, 16: 9, 17: 5, 18: 1}

KIND_STR, KIND_INT, KIND_FLOAT, KIND_VEC3, KIND_VEC4, KIND_INT64, KIND_RAW = \
    "str", "int", "float", "vec3", "vec4", "int64", "raw"


def type_kind(t):
    if t == 0 or t >= 34:
        return KIND_STR
    if t in (1, 7, 8, 9, 10) or 19 <= t <= 30:
        return KIND_INT
    if t in (2, 11):
        return KIND_FLOAT
    if t in (3, 12, 13, 14, 31, 32, 33):
        return KIND_VEC3
    if t in (4, 15, 16, 17, 18):
        return KIND_VEC4
    if t == 5:
        return KIND_INT64
    return KIND_RAW


_F = struct.Struct("<f")


def _read_cvec(buf, p, n):
    flags = buf[p]
    p += 1
    out = []
    for i in range(n):
        m = (flags >> (2 * i)) & 3
        if m == 0:
            out.append(0.0)
        elif m == 1:
            out.append(1.0)
        elif m == 2:
            out.append(-1.0)
        else:
            out.append(_F.unpack_from(buf, p)[0])
            p += 4
    return tuple(out)


def _f32(x):
    return _F.unpack(_F.pack(x))[0]


def _enc_cvec(v):
    """Encode vec with constant-compression -> (type offset count, bytes). Returns (nconst, data)."""
    flags = 0
    body = b""
    nconst = 0
    for i, x in enumerate(v):
        x = _f32(x)
        if x == 0.0 and not str(x).startswith("-"):
            m = 0
        elif x == 1.0:
            m = 1
        elif x == -1.0:
            m = 2
        else:
            m = 3
            body += _F.pack(x)
        if m != 3:
            nconst += 1
        flags |= m << (2 * i)
    return nconst, bytes([flags]) + body


class Attr:
    __slots__ = ("name", "type", "value")

    def __init__(self, name, type_, value):
        self.name = name
        self.type = type_
        self.value = value

    @property
    def kind(self):
        return type_kind(self.type)

    def __repr__(self):
        return "Attr(%r, %s, %r)" % (self.name, TYPE_NAMES.get(self.type, self.type), self.value)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, children=None):
        self.tag = tag
        self.attrs = attrs if attrs is not None else []
        self.children = children if children is not None else []
        self.parent = None

    # -- convenience
    def attr(self, name):
        for a in self.attrs:
            if a.name == name:
                return a
        return None

    def get(self, name, default=None):
        a = self.attr(name)
        return a.value if a else default

    def set(self, name, value, kind=None):
        """Set (or add) an attribute; the binary type is re-chosen on save."""
        a = self.attr(name)
        if a is None:
            if kind is None:
                kind = (KIND_INT if isinstance(value, int) else KIND_FLOAT if isinstance(value, float)
                        else KIND_STR if isinstance(value, str) else KIND_VEC3 if len(value) == 3 else KIND_VEC4)
            a = Attr(name, {KIND_INT: T_INT32, KIND_FLOAT: T_F1, KIND_STR: T_STR, KIND_VEC3: T_F3,
                            KIND_VEC4: T_F4, KIND_INT64: T_INT64, KIND_RAW: T_RAW}[kind], value)
            self.attrs.append(a)
        else:
            if isinstance(value, float) and a.kind == KIND_INT:
                # the game stores whole-number floats compactly as ints; keep that unless fractional
                if value.is_integer():
                    value = int(value)
                else:
                    a.type = T_F1
            a.value = value
        return a

    def child(self, tag):
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def find(self, tag):
        """Depth-first search for first descendant with tag."""
        stack = [self]
        while stack:
            n = stack.pop()
            if n.tag == tag:
                return n
            stack.extend(reversed(n.children))
        return None

    def iter(self):
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.children))

    def add_child(self, node, index=None):
        node.parent = self
        if index is None:
            self.children.append(node)
        else:
            self.children.insert(index, node)
        return node

    def path(self):
        parts = []
        n = self
        while n is not None:
            parts.append(n.tag)
            n = n.parent
        return "/".join(reversed(parts))

    def __repr__(self):
        return "<Node %s attrs=%d children=%d>" % (self.tag, len(self.attrs), len(self.children))


# ---------------------------------------------------------------- XMLCPB reader


def _read_strtable(d, pos, n, size):
    offs = struct.unpack_from("<%dI" % n, d, pos)
    base = pos + 4 * n
    out = []
    for o in offs:
        e = d.index(b"\0", base + o)
        out.append(d[base + o:e].decode("utf-8", "surrogateescape"))
    return out, base + size


class XmlCpb:
    """Decoded XMLCPB document. `root` is the tree; `fields` the original 64-byte header words."""

    def __init__(self, root, fields, tables=None):
        self.root = root
        self.fields = fields
        self.tables = tables  # original (tags, names, strs, sets) for stable re-encoding

    # ---- parse
    @classmethod
    def parse(cls, d, fields):
        (ntags, stags, nnames, snames, nstrs, sstrs, nsets, ssets,
         nodes_size, num_nodes) = fields[5:15]
        nodes = d[:nodes_size]
        pos = nodes_size
        tags, pos = _read_strtable(d, pos, ntags, stags)
        names, pos = _read_strtable(d, pos, nnames, snames)
        strs, pos = _read_strtable(d, pos, nstrs, sstrs)
        cnt = d[pos:pos + nsets]
        soffs = struct.unpack_from("<%dH" % nsets, d, pos + nsets)
        sd = d[pos + 3 * nsets:pos + 3 * nsets + ssets]
        if pos + 3 * nsets + ssets != len(d):
            raise ValueError("XMLCPB size mismatch")
        sets = [struct.unpack_from("<%dH" % cnt[i], sd, soffs[i]) for i in range(nsets)]

        objs = []
        child_refs = []
        p = 0
        nid = 0
        U16 = struct.Struct("<H").unpack_from
        U32 = struct.Struct("<I").unpack_from
        while p < nodes_size:
            h = U16(nodes, p)[0]
            p += 2
            tag = h & 0x3FF
            seq = (h >> 14) & 1
            has_attrs = h >> 15
            nch = (h >> 12) & 3
            starts = []
            consec = False
            if nch == 3:
                c = nodes[p]
                p += 1
                if c & 0x80:
                    nch = ((c << 8) | nodes[p]) & 0x3FFF
                    p += 1
                elif c & 0x40:
                    nch = (c & 7) + 2
                    consec = True
                    starts = [nid - ((c >> 3) & 7) - nch - 1]
                else:
                    nch = c
            blocks = (1 if nch & 0x3F else 0) + (nch >> 6)
            sid = None
            if has_attrs:
                sid = ((h >> 2) & 0x300) | nodes[p]
                p += 1
                if sid == 0x3FF:
                    sid = U16(nodes, p)[0]
                    p += 2
            if consec:
                pass
            elif seq:
                if nch:
                    starts = [nid - nch]
            elif blocks == 1:
                c = nodes[p]
                p += 1
                # the game's reader treats any byte with bit 7 set as the 3-byte form
                if not c & 0x80:
                    dist = c
                else:
                    dist = ((c & 0x3F) << 16) | U16(nodes, p)[0]
                    p += 2
                # dist = gap between the last child and this node
                starts = [nid - dist - nch]
            elif blocks > 1:
                rem = nch
                for _ in range(blocks):
                    c = nodes[p]
                    dist = ((c & 0x3F) << 16) | U16(nodes, p + 1)[0]
                    p += 3
                    bc = min(64, rem)
                    rem -= bc
                    starts.append(nid - bc - dist)
            kids = []
            for i in range(nch):
                kids.append(starts[i >> 6] + (i & 0x3F))
            attrs = []
            if has_attrs:
                for ah in sets[sid]:
                    t = ah & 0x3F
                    name = names[ah >> 6]
                    if t == T_STR:
                        v = strs[U16(nodes, p)[0]]
                        p += 2
                    elif t >= T_CONSTSTR:
                        v = CONST_STRINGS[t - T_CONSTSTR]
                    elif t == T_INT32:
                        v = struct.unpack_from("<i", nodes, p)[0]
                        p += 4
                    elif t == T_F1:
                        v = _F.unpack_from(nodes, p)[0]
                        p += 4
                    elif t == T_F3:
                        v = struct.unpack_from("<3f", nodes, p)
                        p += 12
                    elif t == T_F4:
                        v = struct.unpack_from("<4f", nodes, p)
                        p += 16
                    elif t == T_INT64:
                        v = struct.unpack_from("<q", nodes, p)[0]
                        p += 8
                    elif t == T_RAW:
                        n = U32(nodes, p)[0]
                        v = bytes(nodes[p + 4:p + 4 + n])
                        p += 4 + n
                    elif t == T_POS16:
                        v = U16(nodes, p)[0]
                        p += 2
                    elif t == T_NEG16:
                        v = -U16(nodes, p)[0]
                        p += 2
                    elif t == T_POS8:
                        v = nodes[p]
                        p += 1
                    elif t == T_NEG8:
                        v = -nodes[p]
                        p += 1
                    elif t == T_F1_1DEC:
                        v = struct.unpack_from("<b", nodes, p)[0] * _f32(0.1)
                        v = _f32(v)
                        p += 1
                    elif T_F3_1C <= t <= T_F3_3C:
                        v = _read_cvec(nodes, p, 3)
                        p += _FIXED_SIZE[t]
                    elif T_F4_1C <= t <= T_F4_4C:
                        v = _read_cvec(nodes, p, 4)
                        p += _FIXED_SIZE[t]
                    elif T_0 <= t <= T_10:
                        v = t - T_0
                    elif t == T_255:
                        v = 255
                    elif t == T_F3_100:
                        v = (1.0, 0.0, 0.0)
                    elif t == T_F3_010:
                        v = (0.0, 1.0, 0.0)
                    elif t == T_F3_001:
                        v = (0.0, 0.0, 1.0)
                    else:
                        raise ValueError("unknown attr type %d" % t)
                    attrs.append(Attr(name, t, v))
            objs.append(Node(tags[tag], attrs))
            child_refs.append(kids)
            nid += 1
        if nid != num_nodes or p != nodes_size:
            raise ValueError("node count mismatch (%d/%d)" % (nid, num_nodes))
        for n, kids in zip(objs, child_refs):
            for k in kids:
                c = objs[k]
                c.parent = n
                n.children.append(c)
        root = objs[-1]
        return cls(root, list(fields), (tags, names, strs, sets))

    # ---- write
    def serialize(self):
        """-> (decompressed XMLCPB bytes, header fields)."""
        otags, onames, ostrs, osets = self.tables if self.tables else ([], [], [], [])
        tags = list(otags)
        tag_idx = {s: i for i, s in enumerate(tags)}
        names = list(onames)
        name_idx = {s: i for i, s in enumerate(names)}
        strs = list(ostrs)
        str_idx = {}
        for i, s in enumerate(strs):
            str_idx.setdefault(s, i)
        sets = list(osets)
        set_idx = {}
        for i, s in enumerate(sets):
            set_idx.setdefault(tuple(s), i)

        def intern(tbl, idx, s):
            i = idx.get(s)
            if i is None:
                i = len(tbl)
                tbl.append(s)
                idx[s] = i
            return i

        # node order (mirrors the game's writer): a closed node becomes "pending" in its parent;
        # pending children are flushed in batches of 64, and the rest when the parent closes.
        # Root is written last.
        order = []

        def close(n):
            # iterative version of:
            #   for c in children: close(c); pending.append(c); flush if len(pending) == 64
            #   flush pending
            stack = [(n, 0, [])]
            while stack:
                node, i, pending = stack.pop()
                if i > 0:
                    pending.append(node.children[i - 1])
                    if len(pending) == 64:
                        order.extend(pending)
                        pending = []
                if i < len(node.children):
                    stack.append((node, i + 1, pending))
                    stack.append((node.children[i], 0, []))
                else:
                    order.extend(pending)

        close(self.root)
        order.append(self.root)
        ids = {id(n): i for i, n in enumerate(order)}

        out = bytearray()
        for nid, n in enumerate(order):
            tag = intern(tags, tag_idx, n.tag)
            if tag > 0x3FF:
                raise ValueError("too many tags")
            headers = []
            data = bytearray()
            for a in n.attrs:
                t, blob = _encode_attr(a, strs, str_idx)
                ni = intern(names, name_idx, a.name)
                if ni >= 1024:
                    raise ValueError("too many attribute names")
                headers.append((ni << 6) | t)
                data += blob
            nch = len(n.children)
            h = tag
            extra = bytearray()
            if n.attrs:
                h |= 0x8000
            blocks = (1 if nch & 0x3F else 0) + (nch >> 6)
            starts = []
            for b in range(blocks):
                s = ids[id(n.children[64 * b])]
                for k, c in enumerate(n.children[64 * b:64 * b + 64]):
                    if ids[id(c)] != s + k:
                        raise AssertionError("children block not contiguous")
                starts.append(s)
            dist1 = nid - starts[0] - nch if blocks == 1 else None
            seq = blocks == 1 and dist1 == 0
            consec = blocks == 1 and 3 <= nch <= 9 and 1 <= dist1 <= 8
            if seq:
                h |= 0x4000
            if nch < 3:
                h |= nch << 12
            else:
                h |= 3 << 12
                if consec:
                    extra.append(0x40 | ((dist1 - 1) << 3) | (nch - 2))
                elif nch < 0x40:
                    extra.append(nch)
                elif nch < 0x4000:
                    extra += bytes([0x80 | (nch >> 8), nch & 0xFF])
                else:
                    raise ValueError("too many children")
            if n.attrs:
                sid = intern(sets, set_idx, tuple(headers))
                if sid < 0x3FF:
                    h |= ((sid >> 8) & 3) << 10
                    extra.append(sid & 0xFF)
                else:
                    h |= 3 << 10
                    extra.append(0xFF)
                    extra += struct.pack("<H", sid)
            if nch and not seq and not consec:
                if blocks == 1:
                    if dist1 < 0x80:
                        extra.append(dist1)
                    else:
                        extra += bytes([0xC0 | (dist1 >> 16)]) + struct.pack("<H", dist1 & 0xFFFF)
                else:
                    rem = nch
                    for b in range(blocks):
                        bc = min(64, rem)
                        rem -= bc
                        dist = nid - bc - starts[b]
                        extra += bytes([0xC0 | (dist >> 16)]) + struct.pack("<H", dist & 0xFFFF)
            out += struct.pack("<H", h) + extra + data
        nodes_size = len(out)

        def put_table(lst):
            enc = [s.encode("utf-8", "surrogateescape") + b"\0" for s in lst]
            offs = []
            o = 0
            for e in enc:
                offs.append(o)
                o += len(e)
            out.extend(struct.pack("<%dI" % len(offs), *offs))
            blob = b"".join(enc)
            out.extend(blob)
            return len(lst), len(blob)

        nt, st = put_table(tags)
        nn, sn = put_table(names)
        ns, ss = put_table(strs)
        sd = bytearray()
        soffs = []
        for s in sets:
            soffs.append(len(sd))
            sd += struct.pack("<%dH" % len(s), *s)
        if len(sd) > 0xFFFF:
            raise ValueError("attr set table too large")
        out += bytes(len(s) for s in sets)
        out += struct.pack("<%dH" % len(sets), *soffs)
        out += sd
        f = list(self.fields)
        f[5:15] = [nt, st, nn, sn, ns, ss, len(sets), len(sd), nodes_size, len(order)]
        return bytes(out), f


def _choose_int_type(v):
    if 0 <= v <= 10:
        return T_0 + v, b""
    if v == 255:
        return T_255, b""
    if 0 < v < 256:
        return T_POS8, bytes([v])
    if -256 < v < 0:
        return T_NEG8, bytes([-v])
    if 0 < v < 65536:
        return T_POS16, struct.pack("<H", v)
    if -65536 < v < 0:
        return T_NEG16, struct.pack("<H", -v)
    if -2 ** 31 <= v < 2 ** 31:
        return T_INT32, struct.pack("<i", v)
    return T_INT64, struct.pack("<q", v)


def _encode_attr(a, strs, str_idx):
    """Keep the original binary type if the value still fits it, else pick a new one."""
    t, v = a.type, a.value
    k = type_kind(t)
    if k == KIND_STR:
        v = str(v)
        if t >= T_CONSTSTR and CONST_STRINGS[t - T_CONSTSTR] == v:
            return t, b""
        ci = _CONST_STR_IDX.get(v)
        if ci is not None and t != T_STR:
            return T_CONSTSTR + ci, b""
        i = str_idx.get(v)
        if i is None:
            i = len(strs)
            strs.append(v)
            str_idx[v] = i
        if i > 0xFFFF:
            raise ValueError("string table overflow")
        return T_STR, struct.pack("<H", i)
    if k == KIND_INT:
        v = int(v)
        if t == T_INT32 and -2 ** 31 <= v < 2 ** 31:
            return t, struct.pack("<i", v)
        if t == T_POS16 and 0 <= v < 65536:
            return t, struct.pack("<H", v)
        if t == T_NEG16 and -65536 < v <= 0:
            return t, struct.pack("<H", -v)
        if t == T_POS8 and 0 <= v < 256:
            return t, bytes([v])
        if t == T_NEG8 and -256 < v <= 0:
            return t, bytes([-v])
        if T_0 <= t <= T_10 and v == t - T_0:
            return t, b""
        if t == T_255 and v == 255:
            return t, b""
        return _choose_int_type(v)
    if k == KIND_FLOAT:
        v = float(v)
        if t == T_F1_1DEC:
            b = round(v / _f32(0.1))
            if -128 <= b <= 127 and _f32(b * _f32(0.1)) == _f32(v):
                return t, struct.pack("<b", b)
        return T_F1, _F.pack(v)
    if k in (KIND_VEC3, KIND_VEC4):
        v = tuple(float(x) for x in v)
        n = 3 if k == KIND_VEC3 else 4
        if len(v) != n:
            raise ValueError("%s needs %d components" % (a.name, n))
        if t == T_F3_100 and v == (1.0, 0.0, 0.0) or t == T_F3_010 and v == (0.0, 1.0, 0.0) \
                or t == T_F3_001 and v == (0.0, 0.0, 1.0):
            return t, b""
        if t in (T_F3, T_F4):
            return t, struct.pack("<%df" % n, *v)
        nconst, blob = _enc_cvec(v)
        if nconst == 0:
            return (T_F3 if n == 3 else T_F4), struct.pack("<%df" % n, *v)
        base_t = T_F3_1C if n == 3 else T_F4_1C
        return base_t + nconst - 1, blob
    if k == KIND_INT64:
        return T_INT64, struct.pack("<q", int(v))
    if k == KIND_RAW:
        v = bytes(v)
        return T_RAW, struct.pack("<I", len(v)) + v
    raise ValueError("cannot encode %r" % a)


# ---------------------------------------------------------------- high level


class SaveFile:
    def __init__(self, path=None, raw=None):
        self.path = path
        if raw is None:
            with open(path, "rb") as f:
                raw = f.read()
        data, fields = unpack_container(raw)
        self.doc = XmlCpb.parse(data, fields)

    @property
    def root(self):
        return self.doc.root

    def to_bytes(self):
        data, fields = self.doc.serialize()
        return pack_container(data, fields)

    def save(self, path=None):
        path = path or self.path
        blob = self.to_bytes()
        # sanity: must parse back
        SaveFile(raw=blob)
        with open(path, "wb") as f:
            f.write(blob)
        return path


def to_xml(node, indent=0, max_raw=64):
    """Debug dump of a subtree as XML-ish text."""
    from xml.sax.saxutils import quoteattr
    lines = []

    def fmt(a):
        v = a.value
        if isinstance(v, tuple):
            v = ",".join("%g" % x for x in v)
        elif isinstance(v, float):
            v = "%g" % v
        elif isinstance(v, bytes):
            v = "raw:" + v[:max_raw].hex()
        return "%s=%s" % (a.name, quoteattr(str(v)))

    def rec(n, d):
        pad = "  " * d
        at = " ".join(fmt(a) for a in n.attrs)
        head = "%s<%s%s" % (pad, n.tag, (" " + at) if at else "")
        if n.children:
            lines.append(head + ">")
            for c in n.children:
                rec(c, d + 1)
            lines.append("%s</%s>" % (pad, n.tag))
        else:
            lines.append(head + "/>")

    rec(node, indent)
    return "\n".join(lines)
