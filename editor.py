"""
Prey (2017) Save Editor - GUI.

Run:  python editor.py            (or double-click "Prey Save Editor.pyw")

Tabs:
  Player     - health, psi, key stats
  Inventory  - item stack counts (neuromods, materials, ammo, ...)
  Abilities  - neuromod abilities (acquire / remove)
  Advanced   - full tree of the save: browse, search and edit any value

Every save writes a backup of the original file first (see the Backups folder
next to your SaveGames folder).
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import xml.etree.ElementTree as ET
from tkinter import filedialog, messagebox, simpledialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preysave as ps  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "1.1.0"
SETTINGS_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PreySaveEditor",
                             "settings.json")


def _saved_games_folder():
    """Return the current user's Windows Saved Games known folder, including redirected locations."""
    if os.name == "nt":
        try:
            import ctypes

            class GUID(ctypes.Structure):
                _fields_ = (("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8))

            folder_id = GUID(0x4C5C32FF, 0xBB9D, 0x43B0,
                             (ctypes.c_ubyte * 8)(0xB5, 0xB4, 0x2D, 0x72, 0xE5, 0x4E, 0xAA, 0xA4))
            path = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = (
                ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path)) == 0:
                try:
                    return path.value
                finally:
                    ole32 = ctypes.windll.ole32
                    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
                    ole32.CoTaskMemFree.restype = None
                    ole32.CoTaskMemFree(ctypes.cast(path, ctypes.c_void_p))
        except (AttributeError, OSError, ValueError):
            pass
    return os.path.join(os.path.expanduser("~"), "Saved Games")


PREY_DIR = os.path.join(_saved_games_folder(), "Arkane Studios", "Prey")
SAVE_ROOT = os.path.join(PREY_DIR, "SaveGames")
BACKUP_ROOT = os.path.join(PREY_DIR, "SaveEditorBackups")

try:
    with open(os.path.join(APP_DIR, "abilities.json"), encoding="utf-8") as _f:
        ABILITIES = json.load(_f)
except OSError:
    ABILITIES = {}

try:
    with open(os.path.join(APP_DIR, "items.json"), encoding="utf-8") as _f:
        ITEMS = json.load(_f)
except OSError:
    ITEMS = []
ITEM_BY_ARCH = {i["archetype"]: i for i in ITEMS}
ITEM_BY_ID = {i["id"]: i for i in ITEMS}

# item categories that can't safely be created from scratch (quest items, notes, weapons with complex state)
_NOT_ADDABLE_CATEGORIES = {"Weapons", "MissionItems", "Data"}


def addable_items(chipsets):
    """Catalog entries the editor can create: inventory items (chipsets=False) or chipsets (True)."""
    out = []
    for i in ITEMS:
        if i["deprecated"] or not i["inventory"] or i["archetype"].endswith(".Random"):
            continue
        if i["category"] in _NOT_ADDABLE_CATEGORIES or i["class"].startswith("ArkWeapon") and i["class"] != "ArkWeaponMod":
            continue
        if bool(i.get("chipset")) != chipsets:
            continue
        out.append(i)
    return out


KEY_STATS = ["HitPoints", "PsiPointsPool", "FatigueMax", "InventoryRows", "InventoryColumns",
             "SuitModSlots", "ScopeModSlots", "MaxOxygen", "FlashlightBatteryCapacity",
             "RecyclingYieldScale", "repairDiscount", "MaxSpeedStand", "JumpHeight"]


# ---------------------------------------------------------------- helpers

def fmt_value(a):
    v = a.value
    if isinstance(v, tuple):
        return ", ".join("%g" % x for x in v)
    if isinstance(v, float):
        return "%g" % v
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def parse_value(kind, text, n=None):
    text = text.strip()
    if kind == ps.KIND_STR:
        return text
    if kind in (ps.KIND_INT, ps.KIND_INT64):
        return int(float(text)) if re.fullmatch(r"-?\d+(\.0*)?", text) else int(text)
    if kind == ps.KIND_FLOAT:
        return float(text)
    if kind in (ps.KIND_VEC3, ps.KIND_VEC4):
        parts = [float(x) for x in re.split(r"[,\s]+", text) if x]
        want = 3 if kind == ps.KIND_VEC3 else 4
        if len(parts) != want:
            raise ValueError("expected %d numbers" % want)
        return tuple(parts)
    if kind == ps.KIND_RAW:
        return bytes.fromhex(text)
    raise ValueError(kind)


def game_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Prey.exe"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "Prey.exe" in out
    except OSError:
        return False


# ---------------------------------------------------------------- launching the game

STEAM_APP_ID = "480490"


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def set_save_root(path):
    """Update the active save and backup locations. `path` must be the SaveGames directory."""
    global PREY_DIR, SAVE_ROOT, BACKUP_ROOT
    SAVE_ROOT = os.path.abspath(os.path.normpath(path))
    PREY_DIR = os.path.dirname(SAVE_ROOT)
    BACKUP_ROOT = os.path.join(PREY_DIR, "SaveEditorBackups")


def resolve_save_root(path):
    """Accept SaveGames itself, the Prey directory, or the Saved Games directory."""
    path = os.path.abspath(os.path.normpath(path))
    candidates = (path, os.path.join(path, "SaveGames"),
                  os.path.join(path, "Arkane Studios", "Prey", "SaveGames"))
    for candidate in candidates:
        if os.path.isdir(candidate) and any(
                os.path.isfile(os.path.join(candidate, campaign, slot, "save.CSF"))
                for campaign in os.listdir(candidate)
                if os.path.isdir(os.path.join(candidate, campaign))
                for slot in os.listdir(os.path.join(candidate, campaign))):
            return candidate
    return None


_custom_save_root = load_settings().get("save_root")
if _custom_save_root:
    set_save_root(_custom_save_root)


def _reg_value(root, key, name):
    try:
        import winreg
        with winreg.OpenKey(root, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except (OSError, ImportError):
        return None


def steam_has_prey():
    """True if Steam has Prey installed (so steam://rungameid works)."""
    try:
        import winreg
    except ImportError:
        return False
    steam = _reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
    if not steam:
        return False
    libs = [steam]
    try:
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    return any(os.path.isfile(os.path.join(lib, "steamapps", "appmanifest_%s.acf" % STEAM_APP_ID)) for lib in libs)


def gog_prey_exe():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games") as games:
            for i in range(winreg.QueryInfoKey(games)[0]):
                sub = winreg.EnumKey(games, i)
                with winreg.OpenKey(games, sub) as g:
                    try:
                        name = winreg.QueryValueEx(g, "gameName")[0]
                        path = winreg.QueryValueEx(g, "path")[0]
                    except OSError:
                        continue
                if name.strip().lower() == "prey":
                    exe = os.path.join(path, "Binaries", "Danielle", "x64", "Release", "Prey.exe")
                    if os.path.isfile(exe):
                        return exe
    except (OSError, ImportError):
        pass
    return None


def launch_prey(exe=None):
    """Start the game. Returns a short description of how it was launched."""
    if exe:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
        return exe
    os.startfile("steam://rungameid/%s" % STEAM_APP_ID)
    return "Steam"


def read_meta(slot_dir):
    info = {}
    try:
        root = ET.parse(os.path.join(slot_dir, "save.meta")).getroot()
        info = dict(root.attrib)
    except (OSError, ET.ParseError):
        pass
    return info


def list_slots():
    slots = []
    if not os.path.isdir(SAVE_ROOT):
        return slots
    for camp in sorted(os.listdir(SAVE_ROOT)):
        cdir = os.path.join(SAVE_ROOT, camp)
        if not os.path.isdir(cdir):
            continue
        for slot in os.listdir(cdir):
            sdir = os.path.join(cdir, slot)
            csf = os.path.join(sdir, "save.CSF")
            if os.path.isfile(csf):
                m = read_meta(sdir)
                loc = m.get("location", "").replace("@ui_", "").replace("_", " ")
                when = m.get("saveTime", "")
                try:
                    when = datetime.datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    when = datetime.datetime.fromtimestamp(os.path.getmtime(csf)).strftime("%Y-%m-%d %H:%M")
                slots.append({"path": csf, "label": "%s / %s  -  %s  (%s)" % (camp, slot, loc or "?", when),
                              "mtime": os.path.getmtime(csf)})
    slots.sort(key=lambda s: -s["mtime"])
    return slots


def backup_file(path):
    path = os.path.abspath(path)
    save_root = os.path.abspath(SAVE_ROOT)
    try:
        inside_save_root = os.path.commonpath((path, save_root)) == save_root
    except ValueError:
        inside_save_root = False
    rel = os.path.relpath(path, save_root) if inside_save_root else os.path.basename(path)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dst = os.path.join(BACKUP_ROOT, stamp, rel)
    suffix = 1
    while os.path.exists(dst):
        dst = os.path.join(BACKUP_ROOT, "%s_%d" % (stamp, suffix), rel)
        suffix += 1
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(path, dst)
    return dst


# ---------------------------------------------------------------- model helpers (Prey specific)

class PreyModel:
    def __init__(self, save):
        self.save = save
        root = save.root
        self.player = None
        self.entity_by_id = {}
        self.extra_by_uid = {}
        be = root.find("BasicEntityData")
        if be:
            for n in be.children:
                self.entity_by_id[n.get("id")] = n
        ee = root.find("ExtraEntityData")
        if ee:
            for n in ee.children:
                self.extra_by_uid[n.get("UniqueId")] = n
        for n in root.iter():
            if n.tag == "Extension" and n.get("name") == "ArkPlayer" and n.children:
                self.player = n
                break
        self.player_go = self.player.parent if self.player else None

    def extension(self, name):
        if not self.player_go:
            return None
        for c in self.player_go.children:
            if c.tag == "Extension" and c.get("name") == name:
                return c
        return None

    def stats(self):
        out = []
        sc = self.player.child("ArkStatsComponent") if self.player else None
        if sc:
            for st in sc.children:
                if st.tag == "ArkStat" and st.child("stat") is not None:
                    out.append((st.get("statName"), st.child("stat")))
        return out

    def inventory(self):
        """-> list of dicts: entity id, name, class, count-attr node."""
        items = []
        inv = self.extension("ArkInventory")
        stored = inv.find("storedItems") if inv else None
        if not stored:
            return items
        for i in stored.children:
            v = i.children[0] if i.children else None
            if v is None:
                continue
            eid = v.get("entityId")
            be = self.entity_by_id.get(eid)
            arch = be.get("archetype") if be else None
            cls = be.get("class") if be else None
            uid = be.get("UniqueId") if be else None
            ext = None
            ex = self.extra_by_uid.get(uid) if uid else None
            if ex is None:
                # level-placed entity: find extra data by id suffix
                for k, n in self.extra_by_uid.items():
                    if k and re.search(r"\D%d$" % eid, k):
                        ex = n
                        break
            if ex is not None:
                for n in ex.iter():
                    if n.tag == "Extension" and n.attr("m_count") is not None:
                        ext = n
                        break
            cat = ITEM_BY_ARCH.get(arch) if arch else None
            if cat is None and ext is not None:
                cat = ITEM_BY_ID.get(ext.get("selectedArchetype"))
            arch = arch or (cat["archetype"] if cat else "")
            name = cat["name"] if cat else (arch.replace("ArkPickups.", "") or (ext.get("name") if ext else "?"))
            items.append({"id": eid, "name": name, "archetype": arch,
                          "class": cls or (ext.get("name") if ext else ""), "ext": ext, "slot": v})
        return items

    def abilities(self):
        comp = self.player.child("ArkAbilityComponent") if self.player else None
        lst = comp.child("abilities") if comp else None
        out = []
        if lst:
            for r in lst.iter():
                if r.tag == "ArkAbilityResearch":
                    info = ABILITIES.get(str(r.get("id")), {})
                    out.append((r, info))
        return out

    # ---- creating new items (mirrors an item the player picked up)

    OWNED_FLAGS = (278546, 33554433)  # BasicEntity flags/flags2 of every item carried by the player

    def _level_prefix(self):
        ent = self.player_go
        while ent is not None and ent.tag != "Entity":
            ent = ent.parent
        return re.sub(r"\d+$", "", ent.get("UniqueId")) if ent is not None else "campaign/editor"

    def _new_entity_id(self):
        """Free entity id in the range the game uses for spawned items (counts down from ~65535)."""
        used = {eid & 0xFFFF for eid in self.entity_by_id}
        referenced = set()
        for n in self.save.root.iter():
            for a in n.attrs:
                if isinstance(a.value, int) and 0 < a.value < 1 << 32:
                    referenced.add(a.value & 0xFFFF)
        high = [i for i in used if i >= 60000]
        cand = (min(high) if high else 65001) - 1
        while cand in used or cand in referenced:
            cand -= 1
            if cand < 40000:
                raise RuntimeError("no free entity id found")
        return cand

    def _item_template(self):
        """An Entity (extra data) of a simple item the player carries, to clone the game's own layout."""
        pid = self.player_entity_id()
        for e in self.extra_by_uid.values():
            for x in e.iter():
                if (x.tag == "Extension" and x.attr("m_count") is not None and x.get("ownerId") == pid
                        and not x.get("name", "").startswith("ArkWeapon")
                        and all(c.tag == "activeMods" for c in x.children)):
                    return e
        return None

    def create_item(self, item, count=1):
        """Create a new owned item entity. Returns its entity id."""
        pid = self.player_entity_id()
        eid = self._new_entity_id()
        uid = "%s%d" % (self._level_prefix(), eid)

        be = ps.Node("BasicEntity")
        be.set("id", eid, ps.KIND_INT)
        be.set("flags", self.OWNED_FLAGS[0], ps.KIND_INT)
        be.set("flags2", self.OWNED_FLAGS[1], ps.KIND_INT)
        be.set("class", item["class"], ps.KIND_STR)
        be.set("archetype", item["archetype"], ps.KIND_STR)
        be.set("UniqueId", uid, ps.KIND_STR)

        tpl = self._item_template()
        if tpl is not None:
            ent = tpl.clone()
            ent.set("UniqueId", uid)
        else:  # build the same layout from scratch
            ent = ps.Node("Entity")
            ent.set("UniqueId", uid, ps.KIND_STR)
            proxies = ent.add_child(ps.Node("EntityProxies"))
            proxies.set("numProxies", 4, ps.KIND_INT)
            proxies.add_child(ps.Node("Proxy")).add_child(ps.Node("RenderProxy")).set("SceneMask", 1, ps.KIND_INT)
            proxies.add_child(ps.Node("Proxy")).set("proxyType", 2, ps.KIND_INT)
            p16 = proxies.add_child(ps.Node("Proxy"))
            p16.set("proxyType", 16, ps.KIND_INT)
            go = p16.add_child(ps.Node("GameObject"))
            go.set("updateState", 3, ps.KIND_INT)
            go.set("numExtensions", 1, ps.KIND_INT)
            go.add_child(ps.Node("Extension"))
            p1 = proxies.add_child(ps.Node("Proxy"))
            p1.set("proxyType", 1, ps.KIND_INT)
            phys = p1.add_child(ps.Node("PhysicsProxy"))
            for k, v in (("mass", 1), ("simclass", 1), ("sequenceNumber", 1), ("physDisabled", 64)):
                phys.set(k, v, ps.KIND_INT)
        go = ent.find("GameObject")
        if go is not None:
            go.set("updateState", 3, ps.KIND_INT)  # value used by items in the player's possession
        ext = next(x for x in ent.iter() if x.tag == "Extension")
        ext.attrs = []
        ext.children = []
        ext.set("name", item["class"], ps.KIND_STR)
        ext.set("m_count", int(count), ps.KIND_INT)
        ext.set("selectedArchetype", item["id"], ps.KIND_INT64)
        ext.set("ownerId", pid, ps.KIND_INT)
        if item["class"] in ("ArkRecyclerJunk", "ArkRecyclerJunkExotic"):
            ext.set("junk", 1, ps.KIND_INT)
        if item.get("chipset"):
            ext.set("new", 1, ps.KIND_INT)
            ext.add_child(ps.Node("activeMods"))
        phys = ent.find("PhysicsProxy")
        if phys is not None:
            for a in ("pos", "rot"):
                if phys.attr(a) is not None:
                    phys.attrs.remove(phys.attr(a))
            phys.set("mass", self._num(item.get("mass", 1)), ps.KIND_FLOAT)

        bed = self.save.root.find("BasicEntityData")
        eed = self.save.root.find("ExtraEntityData")
        bed.add_child(be)
        eed.add_child(ent)
        bed.set("BasicEntityDataSize", sum(1 for c in bed.children if c.tag == "BasicEntity"), ps.KIND_INT)
        eed.set("savedEntityCount", sum(1 for c in eed.children if c.tag == "Entity"), ps.KIND_INT)
        self.entity_by_id[eid] = be
        self.extra_by_uid[uid] = ent
        return eid

    def _grid(self):
        inv = self.extension("ArkInventory").child("Inventory")
        stored = inv.child("storedItems")
        if stored is None:
            stored = inv.add_child(ps.Node("storedItems"))
        stats = dict(self.stats())

        def cur(name, default):
            st = stats.get(name)
            return int(st.get("currentValue", st.get("baseValue", default))) if st is not None else default
        cols, rows = cur("InventoryColumns", 9), cur("InventoryRows", 5)
        taken = set()
        for i in self._list_items(stored):
            v = i.child("v")
            if v is None:
                continue
            for dx in range(v.get("width", 1)):
                for dy in range(v.get("height", 1)):
                    taken.add((v.get("x", 1) + dx, v.get("y", 1) + dy))
        return inv, stored, cols, rows, taken

    def item_size(self, item):
        """(w, h) as stored for this item type in the save if the player has one, else the catalog size."""
        for it in self.inventory():
            if it["archetype"] == item["archetype"]:
                return it["slot"].get("width", 1), it["slot"].get("height", 1)
        return item["w"], item["h"]

    def free_spot(self, w, h):
        inv, stored, cols, rows, taken = self._grid()
        for y in range(1, rows - h + 2):
            for x in range(1, cols - w + 2):
                if all((x + dx, y + dy) not in taken for dx in range(w) for dy in range(h)):
                    return x, y
        return None

    def existing_stack(self, item):
        for it in self.inventory():
            if it["archetype"] == item["archetype"] and it["ext"] is not None:
                return it
        return None

    def add_inventory_item(self, item, count):
        """Add `count` of an item. Stackable items join an existing stack if there is one.
        Returns a short description of what happened."""
        if item["stackable"]:
            stack = self.existing_stack(item)
            if stack is not None:
                stack["ext"].set("m_count", stack["ext"].get("m_count", 0) + count, ps.KIND_INT)
                return "added %d to your existing %s stack" % (count, item["name"])
            n_new, per = 1, count
        else:
            n_new, per = count, 1
        w, h = self.item_size(item)
        made = 0
        for _ in range(n_new):
            spot = self.free_spot(w, h)
            if spot is None:
                break
            eid = self.create_item(item, per)
            inv, stored, _, _, _ = self._grid()
            v = stored.add_child(ps.Node("i")).add_child(ps.Node("v"))
            v.set("entityId", eid, ps.KIND_INT)
            v.set("x", spot[0], ps.KIND_INT)
            v.set("y", spot[1], ps.KIND_INT)
            v.set("width", w, ps.KIND_INT)
            v.set("height", h, ps.KIND_INT)
            self._set_size(stored)
            inv.set("numOfItems", len(self._list_items(stored)), ps.KIND_INT)
            made += 1
        if made == 0:
            raise RuntimeError("Your inventory has no free %dx%d space for %s." % (w, h, item["name"]))
        if made < n_new:
            return "added %d of %d %s (inventory full)" % (made, n_new, item["name"])
        return "added %s x%d" % (item["name"], count)

    # ---- chipsets

    def chipset_lists(self, kind):
        """(allChipsets, installedChipsets) nodes for 'suit' or 'scope'."""
        node = self.player.child("SuitMods" if kind == "suit" else "ScopeMods")
        if node is None:
            return None, None
        return node.child("allChipsets"), node.child("installedChipsets")

    def chipsets(self):
        """-> list of dicts for every owned chipset."""
        out = []
        for kind in ("suit", "scope"):
            owned, installed = self.chipset_lists(kind)
            inst = {c.get("v") for c in self._list_items(installed)} if installed is not None else set()
            for c in self._list_items(owned) if owned is not None else []:
                eid = c.get("v")
                be = self.entity_by_id.get(eid)
                ex = self.extra_by_uid.get(be.get("UniqueId")) if be is not None else None
                ext = next((x for x in ex.iter() if x.tag == "Extension"), None) if ex is not None else None
                item = ITEM_BY_ID.get(ext.get("selectedArchetype")) if ext is not None else None
                if item is None and be is not None:
                    item = ITEM_BY_ARCH.get(be.get("archetype"))
                out.append({"id": eid, "kind": kind, "installed": eid in inst,
                            "name": item["name"] if item else (be.get("archetype") if be is not None else "?"),
                            "archetype": item["archetype"] if item else ""})
        return out

    # the game files say scope, but the game itself always files these under suit chipsets
    CHIPSET_KIND_OVERRIDES = {"ArkPickups.Mods.Psychoscope.MechTechs": "suit"}

    def chipset_kind(self, item):
        """'suit' or 'scope': follow where the game already put this chipset type, else the game data."""
        for c in self.chipsets():
            if c["archetype"] == item["archetype"]:
                return c["kind"]
        return self.CHIPSET_KIND_OVERRIDES.get(item["archetype"], item.get("chipset"))

    def add_chipset(self, item):
        kind = self.chipset_kind(item)
        owned, _ = self.chipset_lists(kind)
        if owned is None:
            node = self.player.child("SuitMods" if kind == "suit" else "ScopeMods")
            if node is None:
                raise RuntimeError("This save has no %s chipset data yet." % kind)
            owned = node.add_child(ps.Node("allChipsets"))
        eid = self.create_item(item, 1)
        owned.add_child(ps.Node("i")).set("v", eid, ps.KIND_INT)
        self._set_size(owned)
        return eid

    # ---- perk effects (mirrors what the game does when you buy an ability)

    def player_entity_id(self):
        ent = self.player_go
        while ent is not None and ent.tag != "Entity":
            ent = ent.parent
        uid = ent.get("UniqueId") if ent is not None else None
        for eid, be in self.entity_by_id.items():
            if be.get("UniqueId") == uid:
                return eid
        m = re.search(r"(\d+)$", uid or "")
        return int(m.group(1)) if m else None

    def _stat_by_name(self, name):
        for n, st in self.stats():
            if n.lower() == name.lower():
                return st
        return None

    @staticmethod
    def _list_items(lst):
        return [c for c in lst.children if c.tag == "i"]

    @staticmethod
    def _set_size(lst):
        n = sum(1 for c in lst.children if c.tag == "i")
        a = lst.attr("Size")
        if n:
            lst.set("Size", n, ps.KIND_INT)
        elif a is not None:
            lst.attrs.remove(a)

    def _next_modifier_id(self):
        sc = self.player.child("ArkStatsComponent")
        used = [n.get("id", 0) for n in sc.iter() if n.tag == "Modifier"]
        nid = max([sc.get("currentId", 0)] + used) + 1
        sc.set("currentId", nid, ps.KIND_INT)
        return nid

    @staticmethod
    def _num(v):
        v = float(v)
        return int(v) if v.is_integer() else v

    def _signal_list(self, inbound, create):
        ig = self.save.root.find("IGame")
        if ig is None:
            return None
        tag = "inboundModifiers" if inbound else "outboundModifiers"
        lst = ig.child(tag)
        if lst is None:
            return None
        pid = self.player_entity_id()
        for i in self._list_items(lst):
            v = i.child("v")
            if v is not None and v.get("entity") == pid:
                mods = v.child("modifiers")
                if mods is None and create:
                    mods = v.add_child(ps.Node("modifiers"))
                return mods
        if not create or pid is None:
            return None
        i = lst.add_child(ps.Node("i"))
        v = i.add_child(ps.Node("v"))
        v.set("entity", pid, ps.KIND_INT)
        mods = v.add_child(ps.Node("modifiers"))
        self._set_size(lst)
        return mods

    def _missing_stat_effects(self, r, info):
        """Return configured stat effects that are not represented by this ability's modifier ids."""
        rm = r.child("modifiers")
        ids = {c.get("v") for c in self._list_items(rm)} if rm is not None else set()
        missing = []
        for stat_name, val in info.get("stats", []):
            st = self._stat_by_name(stat_name)
            if st is None:
                continue
            mods = st.child("modifiers")
            found = False
            if mods is not None:
                for item in self._list_items(mods):
                    mod = item.find("Modifier")
                    if mod is not None and mod.get("id") in ids:
                        found = True
                        break
            if not found:
                missing.append((st, val))
        return missing

    def missing_effects(self, r, info):
        """True if an acquired perk is missing the stat/signal modifiers the game would have added."""
        if not r.get("acquired"):
            return False
        if self._missing_stat_effects(r, info):
            return True
        for sig, inbound in info.get("signals", []):
            mods = self._signal_list(inbound, False)
            if mods is None or not any(c.get("v") == sig for c in self._list_items(mods)):
                return True
        return False

    def apply_effects(self, r, info):
        rm = r.child("modifiers")
        if rm is None:
            rm = r.add_child(ps.Node("modifiers"))
        for st, val in self._missing_stat_effects(r, info):
            mid = self._next_modifier_id()
            mods = st.child("modifiers")
            if mods is None:
                mods = st.add_child(ps.Node("modifiers"))
            i = mods.add_child(ps.Node("i"))
            v = i.add_child(ps.Node("v"))
            mod = v.add_child(ps.Node("Modifier"))
            mod.set("id", mid, ps.KIND_INT)
            v = self._num(val)
            mod.set("value", v, ps.KIND_INT if isinstance(v, int) else ps.KIND_FLOAT)
            self._set_size(mods)
            st.set("currentValue", self._num(st.get("currentValue", st.get("baseValue", 0)) + val), ps.KIND_FLOAT)
            ri = rm.add_child(ps.Node("i"))
            ri.set("v", mid, ps.KIND_INT)
        if self._list_items(rm):
            self._set_size(rm)
        for sig, inbound in info.get("signals", []):
            mods = self._signal_list(inbound, True)
            if mods is not None and not any(c.get("v") == sig for c in self._list_items(mods)):
                i = mods.add_child(ps.Node("i"))
                i.set("v", sig, ps.KIND_INT64)
                self._set_size(mods)
        self._sync_max_health()

    def remove_effects(self, r, info):
        rm = r.child("modifiers")
        ids = {c.get("v") for c in self._list_items(rm)} if rm is not None else set()
        if ids:
            for _, st in self.stats():
                mods = st.child("modifiers")
                if mods is None:
                    continue
                for i in list(self._list_items(mods)):
                    mod = i.find("Modifier")
                    if mod is not None and mod.get("id") in ids:
                        st.set("currentValue", self._num(st.get("currentValue", 0) - mod.get("value", 0)),
                               ps.KIND_FLOAT)
                        mods.children.remove(i)
                self._set_size(mods)
            rm.children = [c for c in rm.children if c.tag != "i"]
            self._set_size(rm)
        for sig, inbound in info.get("signals", []):
            mods = self._signal_list(inbound, False)
            if mods is not None:
                mods.children = [c for c in mods.children if not (c.tag == "i" and c.get("v") == sig)]
                self._set_size(mods)
        self._sync_max_health()

    def _sync_max_health(self):
        hp = self._stat_by_name("HitPoints")
        h = self.extension("ArkHealthExtension")
        if hp is not None and h is not None:
            mx = hp.get("currentValue", hp.get("baseValue"))
            if mx is not None:
                h.set("maxHealth", self._num(mx), ps.KIND_FLOAT)
                if h.get("health", 0) > mx:
                    h.set("health", self._num(mx), ps.KIND_FLOAT)

    def set_acquired(self, r, info, on):
        if on:
            r.set("acquired", 1, ps.KIND_INT)
            if r.attr("seen") is None:
                r.set("seen", 1, ps.KIND_INT)
            self.apply_effects(r, info)
        else:
            self.remove_effects(r, info)
            a = r.attr("acquired")
            if a is not None:
                r.attrs.remove(a)
        self.sync_psi_power_levels()

    def sync_psi_power_levels(self):
        """Psi power 'level' = highest acquired PowerLevel - 1, or -1 when none."""
        ppc = self.player.child("ArkPsiComponent")
        ppc = ppc.child("ArkPsiPowerComponent") if ppc else None
        if not ppc:
            return
        best = {}
        for r, info in self.abilities():
            p = info.get("power")
            if p and r.get("acquired"):
                best[p] = max(best.get(p, 0), int(info.get("level") or 1))
        for power_node in ppc.children:
            p = power_node.tag
            if p in ("Empty", "latentPowers"):
                continue
            if not any(info.get("power") == p for info in ABILITIES.values()):
                continue
            lvl = best.get(p, 0) - 1
            a = power_node.attr("level")
            if lvl == 0:
                if a is not None:
                    power_node.attrs.remove(a)
            else:
                power_node.set("level", lvl, ps.KIND_INT)


# ---------------------------------------------------------------- GUI

class EditorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Prey Save Editor %s" % APP_VERSION)
        self.geometry("1180x760")
        self.minsize(900, 600)
        self.save = None
        self.model = None
        self.dirty = False
        self.slots = []
        self._load_request_id = 0
        self._loaded_display = None
        self._build()
        self.refresh_slots()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- layout
    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Subtitle.TLabel", foreground="#606060", font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(14, 7))
        style.configure("Toolbar.TButton", padding=(9, 5))
        style.configure("Treeview", rowheight=25, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook.Tab", padding=(14, 7))

        header = ttk.Frame(self, padding=(12, 10, 12, 6))
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Prey Save Editor", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.file_var = tk.StringVar(value="No save loaded")
        ttk.Label(header, textvariable=self.file_var, style="Subtitle.TLabel", anchor="e").grid(
            row=0, column=2, columnspan=5, sticky="e", padx=(12, 0))

        ttk.Label(header, text="Save slot:").grid(row=1, column=0, sticky="w", pady=(10, 6), padx=(0, 8))
        self.slot_var = tk.StringVar()
        self.slot_combo = ttk.Combobox(header, textvariable=self.slot_var, state="readonly")
        self.slot_combo.grid(row=1, column=1, columnspan=4, sticky="ew", pady=(10, 6))
        self.slot_combo.bind("<Return>", lambda _e: self.load_selected())
        ttk.Button(header, text="Load", command=self.load_selected, style="Toolbar.TButton").grid(
            row=1, column=5, padx=(8, 4), pady=(10, 6))
        ttk.Button(header, text="Refresh", command=self.refresh_slots, style="Toolbar.TButton").grid(
            row=1, column=6, pady=(10, 6))

        actions = ttk.Frame(header)
        actions.grid(row=2, column=0, columnspan=7, sticky="ew")
        actions.columnconfigure(6, weight=1)
        ttk.Button(actions, text="Open file...", command=self.open_file, style="Toolbar.TButton").grid(
            row=0, column=0, padx=(0, 4))
        ttk.Button(actions, text="Save folder...", command=self.choose_save_folder, style="Toolbar.TButton").grid(
            row=0, column=1, padx=4)
        ttk.Button(actions, text="Backups...", command=self.open_backups, style="Toolbar.TButton").grid(
            row=0, column=2, padx=4)
        ttk.Separator(actions, orient="vertical").grid(row=0, column=3, sticky="ns", padx=8)
        ttk.Button(actions, text="Launch Prey", command=self.launch_game, style="Toolbar.TButton").grid(
            row=0, column=4, padx=(0, 4))
        ttk.Button(actions, text="Game location...", command=self.choose_game_exe, style="Toolbar.TButton").grid(
            row=0, column=5)
        self.save_btn = ttk.Button(
            actions, text="Save changes", command=self.do_save, state="disabled", style="Accent.TButton")
        self.save_btn.grid(row=0, column=7, sticky="e")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        self.tab_player = ttk.Frame(self.nb, padding=10)
        self.tab_inv = ttk.Frame(self.nb, padding=10)
        self.tab_ab = ttk.Frame(self.nb, padding=10)
        self.tab_chip = ttk.Frame(self.nb, padding=10)
        self.tab_adv = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.tab_player, text="Player")
        self.nb.add(self.tab_inv, text="Inventory")
        self.nb.add(self.tab_ab, text="Abilities")
        self.nb.add(self.tab_chip, text="Chipsets")
        self.nb.add(self.tab_adv, text="Advanced (all data)")
        self._build_player()
        self._build_inventory()
        self._build_abilities()
        self._build_chipsets()
        self._build_advanced()

        self.status = tk.StringVar(value="Pick a save slot and click Load.  Close Prey before saving.")
        ttk.Separator(self, orient="horizontal").pack(fill="x")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(12, 6)).pack(fill="x")

        self.bind_all("<Control-o>", lambda _e: self._shortcut(self.open_file))
        self.bind_all("<Control-s>", lambda _e: self._shortcut(self.do_save, self.dirty))
        self.bind_all("<F5>", lambda _e: self._shortcut(self.refresh_slots))

    @staticmethod
    def _shortcut(callback, enabled=True):
        if enabled:
            callback()
        return "break"

    def _display_save_path(self, path):
        path = os.path.normpath(path)
        try:
            if os.path.commonpath((os.path.abspath(path), os.path.abspath(SAVE_ROOT))) == os.path.abspath(SAVE_ROOT):
                return os.path.relpath(path, SAVE_ROOT)
        except ValueError:
            pass
        return path

    def _update_file_label(self):
        if self._loaded_display is None:
            self.file_var.set("No save loaded")
        elif self.dirty:
            self.file_var.set("Unsaved changes  •  " + self._loaded_display)
        else:
            self.file_var.set("Loaded  •  " + self._loaded_display)

    def _tree(self, parent, cols, widths, height=None):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=height or 10)
        for c, w in zip(cols, widths):
            tree.heading(c, text=c, command=lambda c=c, t=tree: self._sort(t, c))
            tree.column(c, width=w, anchor="w", stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return frame, tree

    def _sort(self, tree, col, reverse=None):
        rows = [(tree.set(k, col), k) for k in tree.get_children("")]

        def key(r):
            try:
                return (0, float(r[0]))
            except ValueError:
                return (1, r[0].lower())
        rev = getattr(tree, "_sort_rev", {}).get(col, False) if reverse is None else reverse
        rows.sort(key=key, reverse=rev)
        for i, (_, k) in enumerate(rows):
            tree.move(k, "", i)
        tree._sort_rev = getattr(tree, "_sort_rev", {})
        tree._sort_rev[col] = not rev

    # ---- player tab
    def _build_player(self):
        f = self.tab_player
        box = ttk.LabelFrame(f, text="Vitals", padding=8)
        box.pack(fill="x")
        self.pvars = {}
        fields = [("health", "Health"), ("maxHealth", "Max health"), ("psi", "Psi points")]
        for i, (key, label) in enumerate(fields):
            ttk.Label(box, text=label + ":").grid(row=0, column=2 * i, sticky="e", padx=(12 if i else 0, 4))
            v = tk.StringVar()
            ttk.Entry(box, textvariable=v, width=12).grid(row=0, column=2 * i + 1, sticky="w")
            self.pvars[key] = v
        ttk.Button(box, text="Apply", command=self.apply_vitals).grid(row=0, column=7, padx=12)
        ttk.Button(box, text="Fill health & psi to max", command=self.fill_vitals).grid(row=0, column=8)

        ttk.Label(f, text="Player stats  (double-click a value to edit; 'base' is the unmodified value, "
                          "'current' includes neuromod bonuses)", padding=(0, 10, 0, 4)).pack(anchor="w")
        opts = ttk.Frame(f)
        opts.pack(fill="x")
        self.show_all_stats = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Show all stats", variable=self.show_all_stats,
                        command=self.fill_player).pack(side="left")
        fr, self.stat_tree = self._tree(f, ("stat", "base", "current"), (320, 160, 160))
        fr.pack(fill="both", expand=True)
        self.stat_tree.bind("<Double-1>", self.edit_stat)

    def fill_player(self):
        t = self.stat_tree
        t.delete(*t.get_children())
        if not self.model or not self.model.player:
            return
        m = self.model
        h = m.extension("ArkHealthExtension")
        self.pvars["health"].set(fmt_value(h.attr("health")) if h and h.attr("health") else "")
        self.pvars["maxHealth"].set(fmt_value(h.attr("maxHealth")) if h and h.attr("maxHealth") else "")
        psi = m.player.child("ArkPsiComponent")
        self.pvars["psi"].set(str(psi.get("points", 0)) if psi else "")
        self._stat_nodes = {}
        for name, st in m.stats():
            if not self.show_all_stats.get() and name not in KEY_STATS:
                continue
            iid = t.insert("", "end", values=(name, self._num(st.get("baseValue", 0)),
                                              self._num(st.get("currentValue", 0))))
            self._stat_nodes[iid] = st
        if not self.show_all_stats.get():
            order = {n: i for i, n in enumerate(KEY_STATS)}
            rows = sorted(t.get_children(), key=lambda k: order.get(t.set(k, "stat"), 99))
            for i, k in enumerate(rows):
                t.move(k, "", i)

    @staticmethod
    def _num(v):
        return "%g" % v if isinstance(v, float) else str(v)

    def edit_stat(self, ev):
        t = self.stat_tree
        iid = t.identify_row(ev.y)
        col = t.identify_column(ev.x)
        if not iid or col not in ("#2", "#3"):
            return
        st = self._stat_nodes[iid]
        field = "baseValue" if col == "#2" else "currentValue"
        name = t.set(iid, "stat")
        cur = st.get(field, 0)
        s = simpledialog.askstring("Edit stat", "%s  (%s):" % (name, field), initialvalue=self._num(cur), parent=self)
        if s is None:
            return
        try:
            val = float(s)
        except ValueError:
            messagebox.showerror("Invalid", "Enter a number.")
            return
        st.set(field, val, ps.KIND_FLOAT)
        # keep max health in step with HitPoints
        if name == "HitPoints" and field == "currentValue":
            h = self.model.extension("ArkHealthExtension")
            if h is not None:
                h.set("maxHealth", val if not val.is_integer() else int(val), ps.KIND_FLOAT)
        self.mark_dirty()
        self.fill_player()

    def apply_vitals(self):
        if not self.model:
            return
        m = self.model
        try:
            h = m.extension("ArkHealthExtension")
            if h is not None:
                for key in ("health", "maxHealth"):
                    txt = self.pvars[key].get().strip()
                    if txt:
                        h.set(key, float(txt), ps.KIND_FLOAT)
            txt = self.pvars["psi"].get().strip()
            psi = m.player.child("ArkPsiComponent")
            if txt and psi is not None:
                psi.set("points", float(txt), ps.KIND_FLOAT)
        except ValueError:
            messagebox.showerror("Invalid", "Health / psi must be numbers.")
            return
        self.mark_dirty()
        self.fill_player()
        self.status.set("Vitals updated (not saved yet).")

    def fill_vitals(self):
        if not self.model:
            return
        stats = dict(self.model.stats())
        hp = stats.get("HitPoints")
        pool = stats.get("PsiPointsPool")
        if hp is not None:
            self.pvars["maxHealth"].set(self._num(hp.get("currentValue", hp.get("baseValue", 100))))
            self.pvars["health"].set(self.pvars["maxHealth"].get())
        if pool is not None:
            self.pvars["psi"].set(self._num(pool.get("currentValue", pool.get("baseValue", 50))))
        self.apply_vitals()

    # ---- inventory tab
    def _build_inventory(self):
        f = self.tab_inv
        ttk.Label(f, text="Player inventory.  Double-click (or select + Set count) to change a stack size.",
                  padding=(0, 0, 0, 4)).pack(anchor="w")
        fr, self.inv_tree = self._tree(f, ("item", "type", "count", "entity id"), (300, 320, 90, 90))
        fr.pack(fill="both", expand=True)
        self.inv_tree.bind("<Double-1>", lambda e: self.set_count())
        bar = ttk.Frame(f, padding=(0, 6, 0, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Set count...", command=self.set_count).pack(side="left")
        ttk.Button(bar, text="Add item...", command=lambda: AddItemDialog(self, chipsets=False)).pack(side="left", padx=6)
        ttk.Label(bar, text="   Quick:").pack(side="left")
        ttk.Button(bar, text="Neuromods...", command=lambda: self.quick_set("Neuromod")).pack(side="left", padx=2)
        ttk.Button(bar, text="All crafting materials...",
                   command=lambda: self.quick_set("Crafting.Ingredients")).pack(side="left", padx=2)
        ttk.Button(bar, text="All ammo...", command=lambda: self.quick_set("Ammo.")).pack(side="left", padx=2)
        self.inv_note = ttk.Label(f, foreground="#666", padding=(0, 6, 0, 0), text=(
            "Add item... can give you any non-weapon item (weapons, quest items and notes have to be found "
            "in-game). Chipsets are on the Chipsets tab."))
        self.inv_note.pack(anchor="w")

    def fill_inventory(self):
        t = self.inv_tree
        t.delete(*t.get_children())
        self._inv = {}
        if not self.model:
            return
        for it in self.model.inventory():
            cnt = it["ext"].get("m_count") if it["ext"] is not None else ""
            iid = t.insert("", "end", values=(it["name"], it["archetype"].replace("ArkPickups.", "") or it["class"],
                                              cnt, it["id"]))
            self._inv[iid] = it

    def _apply_count(self, it, n):
        it["ext"].set("m_count", n, ps.KIND_INT)

    def set_count(self):
        sel = self.inv_tree.selection()
        if not sel:
            messagebox.showinfo("Inventory", "Select an item first.")
            return
        it = self._inv[sel[0]]
        if it["ext"] is None:
            messagebox.showerror("Inventory", "No count data found for this item.")
            return
        cur = it["ext"].get("m_count")
        n = simpledialog.askinteger("Set count", "New count for %s:" % it["name"], initialvalue=cur,
                                    minvalue=1, maxvalue=999999, parent=self)
        if n is None:
            return
        for iid in sel:
            if self._inv[iid]["ext"] is not None:
                self._apply_count(self._inv[iid], n)
        self.mark_dirty()
        self.fill_inventory()

    def quick_set(self, needle):
        if not self.model:
            return
        items = [it for it in self._inv.values() if needle.lower() in it["archetype"].lower() and it["ext"] is not None]
        if not items:
            messagebox.showinfo("Inventory", "You aren't carrying any '%s' items.\nPick one up in-game first."
                                % needle.strip("."))
            return
        n = simpledialog.askinteger("Set count", "Set count for %d stack(s):\n%s" % (
            len(items), "\n".join(it["name"] for it in items)), initialvalue=max(
            it["ext"].get("m_count") for it in items), minvalue=1, maxvalue=999999, parent=self)
        if n is None:
            return
        for it in items:
            self._apply_count(it, n)
        self.mark_dirty()
        self.fill_inventory()

    # ---- abilities tab
    def _build_abilities(self):
        f = self.tab_ab
        ttk.Label(f, wraplength=820, justify="left", padding=(0, 0, 0, 6), text=(
            "Neuromod abilities.  Double-click (or select several + Toggle) to acquire / remove.  The editor applies "
            "the same effects the game does when you buy a perk: stat bonuses (inventory size, health, suit mod "
            "slots, ...), damage-resistance/weapon signal modifiers, and psi power levels.  'effects' shows "
            "MISSING for perks that are marked acquired but whose bonuses were never applied.")).pack(anchor="w")
        fr, self.ab_tree = self._tree(f, ("ability", "psi power", "cost", "acquired", "effects"),
                                      (300, 180, 70, 90, 220))
        fr.pack(fill="both", expand=True)
        self.ab_tree.tag_configure("missing", foreground="#b00020")
        self.ab_tree.bind("<Double-1>", lambda e: self.toggle_ability())
        bar = ttk.Frame(f, padding=(0, 6, 0, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Toggle acquired", command=self.toggle_ability).pack(side="left")
        ttk.Button(bar, text="Fix missing perk effects", command=self.fix_perks).pack(side="left", padx=6)
        self.ab_summary = ttk.Label(bar, padding=(12, 0))
        self.ab_summary.pack(side="left")

    def fill_abilities(self):
        t = self.ab_tree
        t.delete(*t.get_children())
        self._ab = {}
        if not self.model:
            return
        n_acq = n_missing = 0
        for r, info in self.model.abilities():
            acq = bool(r.get("acquired"))
            n_acq += acq
            missing = self.model.missing_effects(r, info)
            n_missing += missing
            if missing:
                eff = "MISSING"
            elif acq and (info.get("stats") or info.get("signals")):
                eff = "applied"
            else:
                eff = ""
            desc = ", ".join("%s %+g" % (s, v) for s, v in info.get("stats", []))
            if info.get("signals"):
                desc = (desc + ", " if desc else "") + "signal mod"
            iid = t.insert("", "end", values=(info.get("name", r.get("id")), info.get("power", ""),
                                              info.get("cost", ""), "yes" if acq else "",
                                              eff + ("  (" + desc + ")" if desc and eff else "")),
                           tags=("missing",) if missing else ())
            self._ab[iid] = (r, info)
        self.ab_summary.config(text="%d of %d acquired%s" % (
            n_acq, len(self._ab), ("   -   %d with MISSING effects" % n_missing) if n_missing else ""))

    def _refill_abilities_keep_scroll(self):
        ys = self.ab_tree.yview()
        self.fill_abilities()
        self.ab_tree.yview_moveto(ys[0])
        self.fill_player()

    def toggle_ability(self):
        sel = self.ab_tree.selection()
        if not sel:
            return
        for iid in sel:
            r, info = self._ab[iid]
            self.model.set_acquired(r, info, not r.get("acquired"))
        self.mark_dirty()
        self._refill_abilities_keep_scroll()

    def fix_perks(self):
        if not self.model:
            return
        fixed = [info.get("name") for r, info in self.model.abilities() if self.model.missing_effects(r, info)]
        if not fixed:
            messagebox.showinfo("Perks", "All acquired perks already have their effects applied.")
            return
        for r, info in self.model.abilities():
            if self.model.missing_effects(r, info):
                self.model.apply_effects(r, info)
        self.model.sync_psi_power_levels()
        self.mark_dirty()
        self._refill_abilities_keep_scroll()
        messagebox.showinfo("Perks", "Applied missing effects for %d perk(s):\n%s\n\nClick 'Save changes' to "
                                     "write the save." % (len(fixed), "\n".join(fixed)))

    # ---- chipsets tab
    def _build_chipsets(self):
        f = self.tab_chip
        ttk.Label(f, wraplength=820, justify="left", padding=(0, 0, 0, 6), text=(
            "Suit and Psychoscope chipsets you own.  Add chipset... gives you new ones (uninstalled). "
            "Install them in-game from the Suit/Scope Chipsets screen so the game applies their effects. "
            "Chipset slots come from the Suit Modification (suit) and Psychotronics (scope) abilities.")).pack(anchor="w")
        fr, self.chip_tree = self._tree(f, ("chipset", "type", "installed", "entity id"), (380, 120, 90, 90))
        fr.pack(fill="both", expand=True)
        bar = ttk.Frame(f, padding=(0, 6, 0, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Add chipset...", command=lambda: AddItemDialog(self, chipsets=True)).pack(side="left")
        self.chip_summary = ttk.Label(bar, padding=(12, 0))
        self.chip_summary.pack(side="left")

    def fill_chipsets(self):
        t = self.chip_tree
        t.delete(*t.get_children())
        if not self.model or not self.model.player:
            self.chip_summary.config(text="")
            return
        chips = self.model.chipsets()
        for c in chips:
            t.insert("", "end", values=(c["name"], c["kind"], "yes" if c["installed"] else "", c["id"]))
        n_suit = sum(1 for c in chips if c["kind"] == "suit")
        self.chip_summary.config(text="%d suit, %d scope chipsets" % (n_suit, len(chips) - n_suit))

    # ---- advanced tab
    def _build_advanced(self):
        f = self.tab_adv
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Label(bar, text="Search (tag, attribute name or value):").pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.search_var, width=40)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.do_search())
        ttk.Button(bar, text="Find next", command=self.do_search).pack(side="left")
        ttk.Button(bar, text="Export XML...", command=self.export_xml).pack(side="right")

        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=(6, 0))
        left = ttk.Frame(pw)
        right = ttk.Frame(pw)
        pw.add(left, weight=3)
        pw.add(right, weight=2)
        self.node_tree = ttk.Treeview(left, show="tree")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.node_tree.yview)
        self.node_tree.configure(yscrollcommand=sb.set)
        self.node_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.node_tree.bind("<<TreeviewOpen>>", self._on_open)
        self.node_tree.bind("<<TreeviewSelect>>", self._on_select)

        ttk.Label(right, text="Attributes (double-click value to edit)").pack(anchor="w")
        fr, self.attr_tree = self._tree(right, ("name", "type", "value"), (150, 80, 260))
        fr.pack(fill="both", expand=True)
        self.attr_tree.bind("<Double-1>", self.edit_attr)
        self.path_label = ttk.Label(right, foreground="#666", wraplength=420, justify="left")
        self.path_label.pack(anchor="w", pady=4)

    def fill_advanced(self):
        t = self.node_tree
        t.delete(*t.get_children())
        self._nodes = {}
        self._iid_of = {}
        self._search_iter = None
        if self.save:
            self._insert_node("", self.save.root)

    def _label(self, n):
        bits = []
        for a in n.attrs[:3]:
            bits.append("%s=%s" % (a.name, fmt_value(a)[:40]))
        extra = ("  [%s]" % ", ".join(bits)) if bits else ""
        kids = ("  (%d)" % len(n.children)) if n.children else ""
        return n.tag + extra + kids

    def _insert_node(self, parent_iid, n):
        iid = self.node_tree.insert(parent_iid, "end", text=self._label(n))
        self._nodes[iid] = n
        self._iid_of[id(n)] = iid
        if n.children:
            self.node_tree.insert(iid, "end", text="...", tags=("dummy",))
        return iid

    def _expand(self, iid):
        kids = self.node_tree.get_children(iid)
        if len(kids) == 1 and "dummy" in self.node_tree.item(kids[0], "tags"):
            self.node_tree.delete(kids[0])
            for c in self._nodes[iid].children:
                self._insert_node(iid, c)

    def _on_open(self, ev):
        self._expand(self.node_tree.focus())

    def _on_select(self, ev):
        sel = self.node_tree.selection()
        t = self.attr_tree
        t.delete(*t.get_children())
        self._attr_rows = {}
        if not sel:
            return
        n = self._nodes.get(sel[0])
        if n is None:
            return
        for a in n.attrs:
            iid = t.insert("", "end", values=(a.name, ps.TYPE_NAMES.get(a.type, a.type) if a.type < 34 else "CONST",
                                              fmt_value(a)[:500]))
            self._attr_rows[iid] = (n, a)
        self.path_label.config(text=n.path())

    def edit_attr(self, ev):
        iid = self.attr_tree.identify_row(ev.y)
        if not iid:
            return
        n, a = self._attr_rows[iid]
        kind = a.kind
        hint = {ps.KIND_VEC3: " (x, y, z)", ps.KIND_VEC4: " (x, y, z, w)", ps.KIND_RAW: " (hex)"}.get(kind, "")
        s = simpledialog.askstring("Edit attribute", "%s  [%s]%s:" % (a.name, kind, hint),
                                   initialvalue=fmt_value(a), parent=self)
        if s is None:
            return
        try:
            val = parse_value(kind, s)
        except ValueError as e:
            if kind == ps.KIND_INT:
                try:
                    val = float(s)
                    kind = ps.KIND_FLOAT
                    a.type = ps.T_F1
                except ValueError:
                    messagebox.showerror("Invalid value", str(e))
                    return
            else:
                messagebox.showerror("Invalid value", str(e))
                return
        a.value = val
        self.mark_dirty()
        self._on_select(None)
        sel = self.node_tree.selection()
        if sel:
            self.node_tree.item(sel[0], text=self._label(n))

    def _reveal(self, n):
        chain = []
        x = n
        while x is not None:
            chain.append(x)
            x = x.parent
        for x in reversed(chain[1:]):
            iid = self._iid_of.get(id(x))
            self._expand(iid)
            self.node_tree.item(iid, open=True)
        iid = self._iid_of[id(n)]
        self.node_tree.selection_set(iid)
        self.node_tree.focus(iid)
        self.node_tree.see(iid)

    def do_search(self):
        if not self.save:
            return
        q = self.search_var.get().strip().lower()
        if not q:
            return
        if getattr(self, "_search_q", None) != q or self._search_iter is None:
            self._search_q = q

            def gen():
                for n in self.save.root.iter():
                    if q in n.tag.lower() or any(q in a.name.lower() or q in fmt_value(a).lower()
                                                 for a in n.attrs):
                        yield n
            self._search_iter = gen()
        n = next(self._search_iter, None)
        if n is None:
            self._search_iter = None
            self.status.set("No more matches for '%s' (search wraps on next click)." % q)
            return
        self._reveal(n)
        self.status.set("Found: " + n.path())

    def export_xml(self):
        if not self.save:
            return
        p = filedialog.asksaveasfilename(defaultextension=".xml", filetypes=[("XML", "*.xml")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(ps.to_xml(self.save.root))
            self.status.set("Exported to " + p)

    # ---- load / save
    def refresh_slots(self):
        self.slots = list_slots()
        self.slot_combo["values"] = [s["label"] for s in self.slots]
        if self.slots and not self.slot_var.get():
            self.slot_combo.current(0)
        elif not self.slots:
            self.slot_var.set("")
            self.status.set("No saves found in %s. Use 'Save folder...' for a custom location." % SAVE_ROOT)

    def load_selected(self):
        i = self.slot_combo.current()
        if i < 0:
            return
        self.load_path(self.slots[i]["path"])

    def open_file(self):
        p = filedialog.askopenfilename(initialdir=SAVE_ROOT if os.path.isdir(SAVE_ROOT) else None,
                                       filetypes=[("Prey saves", "*.CSF *.level"), ("All files", "*.*")])
        if p:
            self.load_path(p)

    def load_path(self, path, target=None):
        """Load `path`. If `target` is given (loading a backup), Save writes to `target` instead."""
        if self.dirty and not messagebox.askyesno("Unsaved changes", "Discard unsaved changes?"):
            return
        self.status.set("Loading %s ..." % path)
        self.file_var.set("Loading  •  " + self._display_save_path(path))
        self.config(cursor="watch")
        self.update_idletasks()
        self._load_request_id += 1
        request_id = self._load_request_id

        def work():
            try:
                save = ps.SaveFile(os.path.normpath(path))
                err = None
            except Exception as e:  # noqa: BLE001
                save, err = None, e
            if save is not None and target:
                save.path = os.path.normpath(target)
            self.after(0, lambda: self._loaded(request_id, path, save, err, target))
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, request_id, path, save, err, target=None):
        if request_id != self._load_request_id:
            return
        self.config(cursor="")
        if err:
            messagebox.showerror("Load failed", "%s\n\n%s" % (path, err))
            self.status.set("Load failed.")
            self._update_file_label()
            return
        self.save = save
        self.model = PreyModel(save)
        self.dirty = False
        self._loaded_display = self._display_save_path(save.path)
        self._update_file_label()
        self.save_btn.config(state="disabled")
        self.fill_player()
        self.fill_inventory()
        self.fill_abilities()
        self.fill_chipsets()
        self.fill_advanced()
        self.title("Prey Save Editor %s — %s" % (APP_VERSION, self._loaded_display))
        msg = "Loaded %s" % path
        if target:
            self.mark_dirty()
            msg = "Loaded BACKUP %s  -  'Save changes' will write it to %s" % (path, target)
        if not self.model.player:
            msg += "   (no player data in this file - use the Advanced tab; player data lives in save.CSF)"
        self.status.set(msg)

    def mark_dirty(self):
        self.dirty = True
        self._update_file_label()
        self.save_btn.config(state="normal")
        self.status.set("Unsaved changes.")

    def do_save(self):
        if not self.save:
            return
        if game_running() and not messagebox.askyesno(
                "Prey is running", "Prey appears to be running. It may overwrite or ignore your edits.\n"
                                   "Save anyway?"):
            return
        path = self.save.path
        try:
            bak = backup_file(path)
            self.save.save(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Save failed", str(e))
            return
        self.dirty = False
        self._loaded_display = self._display_save_path(path)
        self._update_file_label()
        self.save_btn.config(state="disabled")
        self.status.set("Saved %s   (backup: %s)" % (path, bak))
        messagebox.showinfo("Saved", "Save written.\n\nBackup of the original:\n%s" % bak)

    def choose_save_folder(self):
        """Select and remember a nonstandard Prey SaveGames location."""
        selected = filedialog.askdirectory(
            title="Select Prey's SaveGames folder (or its parent Prey / Saved Games folder)",
            initialdir=SAVE_ROOT if os.path.isdir(SAVE_ROOT) else _saved_games_folder())
        if not selected:
            return
        root = resolve_save_root(selected)
        if root is None:
            messagebox.showerror(
                "Save folder",
                "No Prey save.CSF files were found there.\n\nSelect SaveGames, the Arkane Studios\\Prey folder, "
                "or the Windows Saved Games folder containing your Prey saves.")
            return
        settings = load_settings()
        settings["save_root"] = root
        save_settings(settings)
        set_save_root(root)
        self.refresh_slots()
        if self.slots:
            self.slot_combo.current(0)
        self.status.set("Using save folder: " + SAVE_ROOT)

    def choose_game_exe(self):
        """Ask for Prey.exe and remember it. Returns the path or None."""
        settings = load_settings()
        cur = settings.get("game_exe")
        p = filedialog.askopenfilename(
            title="Select Prey.exe (usually ...\\Prey\\Binaries\\Danielle\\x64\\Release\\Prey.exe)",
            initialdir=os.path.dirname(cur) if cur else None,
            filetypes=[("Prey.exe", "Prey.exe"), ("Programs", "*.exe")])
        if not p:
            return None
        p = os.path.normpath(p)
        settings["game_exe"] = p
        save_settings(settings)
        self.status.set("Launch Prey will use: " + p)
        return p

    def launch_game(self):
        if game_running():
            messagebox.showinfo("Launch Prey", "Prey is already running.")
            return
        if self.dirty:
            ans = messagebox.askyesnocancel("Unsaved changes", "Save your changes before launching Prey?\n\n"
                                                              "(The game won't see edits you haven't saved.)")
            if ans is None:
                return
            if ans:
                self.do_save()
                if self.dirty:
                    return
        exe = load_settings().get("game_exe")
        if exe and not os.path.isfile(exe):
            exe = None
        if not exe:
            exe = gog_prey_exe()
        if not exe and not steam_has_prey():
            messagebox.showinfo("Launch Prey", "Couldn't find Prey automatically.\n\nPlease select Prey.exe - it's "
                                               "in your Prey folder under Binaries\\Danielle\\x64\\Release.\n"
                                               "This is only asked once (use the '...' button to change it).")
            exe = self.choose_game_exe()
            if not exe:
                return
        try:
            how = launch_prey(exe)
        except OSError as e:
            messagebox.showerror("Launch Prey", "Couldn't start the game:\n%s" % e)
            return
        self.status.set("Launching Prey (%s) ..." % how)

    def current_target(self):
        if self.save:
            return self.save.path
        i = self.slot_combo.current()
        return os.path.normpath(self.slots[i]["path"]) if i >= 0 else None

    def open_backups(self):
        target = self.current_target()
        if not target:
            messagebox.showinfo("Backups", "Select a save slot first.")
            return
        BackupDialog(self, target)

    def on_close(self):
        if self.dirty and not messagebox.askyesno("Unsaved changes", "Quit without saving?"):
            return
        self.destroy()


def list_backups(target):
    """All backups of `target` (a save.CSF / .level path), newest first."""
    target = os.path.normpath(target)
    try:
        rel = os.path.relpath(target, SAVE_ROOT)
    except ValueError:
        return []
    if rel.startswith(".."):
        rel = os.path.basename(target)
    out = []
    if os.path.isdir(BACKUP_ROOT):
        for stamp in os.listdir(BACKUP_ROOT):
            p = os.path.join(BACKUP_ROOT, stamp, rel)
            if os.path.isfile(p):
                try:
                    when = datetime.datetime.strptime(stamp[:22], "%Y%m%d_%H%M%S_%f")
                except ValueError:
                    try:
                        when = datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
                    except ValueError:
                        when = datetime.datetime.fromtimestamp(os.path.getmtime(p))
                out.append({"path": p, "when": when, "kind": "before editor save"})
    for d in os.listdir(PREY_DIR) if os.path.isdir(PREY_DIR) else []:
        if d.startswith("SaveGames_backup_"):
            p = os.path.join(PREY_DIR, d, rel)
            if os.path.isfile(p):
                try:
                    when = datetime.datetime.strptime(d[len("SaveGames_backup_"):], "%Y%m%d_%H%M%S")
                except ValueError:
                    when = datetime.datetime.fromtimestamp(os.path.getmtime(p))
                out.append({"path": p, "when": when, "kind": "full SaveGames backup"})
    out.sort(key=lambda b: b["when"], reverse=True)
    if out:
        oldest = min(out, key=lambda b: b["when"])
        oldest["kind"] += "  (oldest = unedited original)"
    return out


class AddItemDialog(tk.Toplevel):
    """Pick items (or chipsets) from the game's catalog and add them to the loaded save."""

    def __init__(self, app, chipsets):
        if not app.model or not app.model.player:
            messagebox.showinfo("Add", "Load a save.CSF first.")
            return
        super().__init__(app)
        self.app = app
        self.model = app.model
        self.chipsets = chipsets
        self.title("Add chipset" if chipsets else "Add item")
        self.geometry("860x560")
        self.minsize(720, 480)
        self.transient(app)
        self.grab_set()
        self.items = addable_items(chipsets)
        if chipsets:
            owned = {}
            for c in self.model.chipsets():
                owned[c["archetype"]] = owned.get(c["archetype"], 0) + 1
        else:
            owned = {}
            for it in self.model.inventory():
                if it["ext"] is not None:
                    owned[it["archetype"]] = owned.get(it["archetype"], 0) + (it["ext"].get("m_count") or 0)
        self.owned = owned

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        self.q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.q, width=30)
        e.pack(side="left", padx=4)
        e.focus_set()
        self.q.trace_add("write", lambda *a: self.refresh())
        ttk.Label(top, text="  Category:").pack(side="left")
        cats = ["All"] + sorted({self._cat(i) for i in self.items})
        self.cat = tk.StringVar(value="All")
        cb = ttk.Combobox(top, textvariable=self.cat, values=cats, state="readonly", width=16)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        cols = ("name", "category", "size", "you have") if not chipsets else ("name", "type", "you have")
        widths = (330, 200, 60, 90) if not chipsets else (400, 120, 90)
        fr, self.tree = app._tree(self, cols, widths, height=16)
        fr.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<Double-1>", lambda e: self.add())

        bar = ttk.Frame(self, padding=12)
        bar.pack(fill="x")
        ttk.Label(bar, text="Count:").pack(side="left")
        self.count = tk.IntVar(value=1 if chipsets else 10)
        ttk.Spinbox(bar, from_=1, to=999999, textvariable=self.count, width=8).pack(side="left", padx=4)
        ttk.Button(bar, text="Add selected", command=self.add, style="Accent.TButton").pack(side="left", padx=8)
        ttk.Button(bar, text="Close", command=self.destroy, style="Toolbar.TButton").pack(side="right")
        self.msg = ttk.Label(self, padding=(12, 0, 12, 10), foreground="#666", wraplength=800,
                             justify="left", text=(
            "Chipsets are added uninstalled; install them in-game." if chipsets else
            "Stackable items are added to your existing stack if you have one; otherwise they go into the first "
            "free inventory slot. Tip: multi-select with Ctrl/Shift."))
        self.msg.pack(anchor="w")
        self.rows = {}
        self.refresh()
        self.bind("<Escape>", lambda _e: self.destroy())

    def _cat(self, i):
        if self.chipsets:
            return self.model.chipset_kind(i)
        parts = i["archetype"].split(".")
        return ".".join(parts[1:3]) if len(parts) > 3 else parts[1]

    def refresh(self):
        t = self.tree
        t.delete(*t.get_children())
        self.rows = {}
        q = self.q.get().strip().lower()
        cat = self.cat.get()
        for i in sorted(self.items, key=lambda i: (self._cat(i), i["name"].lower())):
            c = self._cat(i)
            if cat != "All" and c != cat:
                continue
            if q and q not in i["name"].lower() and q not in i["archetype"].lower():
                continue
            have = self.owned.get(i["archetype"], "")
            vals = ((i["name"], c, "%dx%d" % (i["w"], i["h"]), have) if not self.chipsets
                    else (i["name"], c, have))
            self.rows[t.insert("", "end", values=vals)] = i

    def add(self):
        sel = [self.rows[s] for s in self.tree.selection() if s in self.rows]
        if not sel:
            messagebox.showinfo("Add", "Select one or more entries first.", parent=self)
            return
        try:
            n = int(self.count.get())
        except (tk.TclError, ValueError):
            n = 0
        if n < 1:
            messagebox.showerror("Add", "Count must be at least 1.", parent=self)
            return
        done, errors = [], []
        for item in sel:
            try:
                if self.chipsets:
                    for _ in range(n):
                        self.model.add_chipset(item)
                    done.append("%s x%d" % (item["name"], n))
                    self.owned[item["archetype"]] = self.owned.get(item["archetype"], 0) + n
                else:
                    done.append(self.model.add_inventory_item(item, n))
                    self.owned[item["archetype"]] = self.owned.get(item["archetype"], 0) + n
            except RuntimeError as e:
                errors.append(str(e))
        if done:
            self.app.mark_dirty()
            self.app.fill_inventory()
            self.app.fill_chipsets()
            self.app.fill_player()
            self.refresh()
        text = ("Added: " + "; ".join(done) + ".  Click 'Save changes' to write the save.") if done else ""
        if errors:
            text += ("\n" if text else "") + "Problems: " + " ".join(errors)
        self.msg.config(text=text, foreground="#b00020" if errors else "#1a7f37")


class BackupDialog(tk.Toplevel):
    def __init__(self, app, target):
        super().__init__(app)
        self.app = app
        self.target = target
        self.title("Backups of " + os.path.relpath(target, SAVE_ROOT) if target.startswith(SAVE_ROOT) else target)
        self.geometry("900x380")
        self.transient(app)
        ttk.Label(self, padding=8, wraplength=860, justify="left", text=(
            "Backups of:  %s\n\nLoad into editor: opens the backup so you can look at it or edit it; "
            "'Save changes' then writes it over the slot.   Restore to slot: copies the backup straight back "
            "(the current file is backed up first)." % target)).pack(anchor="w")
        fr, self.tree = app._tree(self, ("backed up", "type", "file"), (150, 260, 460), height=10)
        fr.pack(fill="both", expand=True, padx=8)
        self.tree.bind("<Double-1>", lambda e: self.load())
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Load into editor", command=self.load).pack(side="left")
        ttk.Button(bar, text="Restore to slot", command=self.restore).pack(side="left", padx=6)
        ttk.Button(bar, text="Open backups folder", command=self.open_folder).pack(side="right")
        self.rows = {}
        for b in list_backups(target):
            iid = self.tree.insert("", "end", values=(b["when"].strftime("%Y-%m-%d %H:%M:%S"), b["kind"], b["path"]))
            self.rows[iid] = b
        if not self.rows:
            self.tree.insert("", "end", values=("", "no backups yet for this file", ""))
        else:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)

    def _selected(self):
        sel = self.tree.selection()
        b = self.rows.get(sel[0]) if sel else None
        if b is None:
            messagebox.showinfo("Backups", "Select a backup first.", parent=self)
        return b

    def load(self):
        b = self._selected()
        if b:
            self.destroy()
            self.app.load_path(b["path"], target=self.target)

    def restore(self):
        b = self._selected()
        if not b:
            return
        if game_running() and not messagebox.askyesno(
                "Prey is running", "Prey appears to be running. Restore anyway?", parent=self):
            return
        if not messagebox.askyesno("Restore backup", "Overwrite\n%s\nwith the backup from %s?" % (
                self.target, b["when"].strftime("%Y-%m-%d %H:%M:%S")), parent=self):
            return
        try:
            ps.SaveFile(b["path"])  # make sure the backup is valid
            cur = backup_file(self.target) if os.path.isfile(self.target) else None
            shutil.copy2(b["path"], self.target)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Restore failed", str(e), parent=self)
            return
        self.destroy()
        self.app.dirty = False
        self.app.load_path(self.target)
        messagebox.showinfo("Restored", "Backup restored.%s" % (
            ("\n\nThe file it replaced was backed up to:\n" + cur) if cur else ""))

    def open_folder(self):
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        os.startfile(BACKUP_ROOT)



def main():
    app = EditorApp()
    if len(sys.argv) > 1:
        app.after(100, lambda: app.load_path(sys.argv[1]))
    app.mainloop()


if __name__ == "__main__":
    main()
