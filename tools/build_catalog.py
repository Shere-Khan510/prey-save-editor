"""
Regenerate items.json and abilities.json from Prey's game data.

Needs the game's XML files extracted, e.g. by Chairloader (its PreyFiles folder):
    python tools/build_catalog.py "<Chairloader PreyFiles folder>"
"""
import json
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)


def s64(u):
    u = int(u)
    return u - 2 ** 64 if u >= 2 ** 63 else u


def local(tag):
    return tag.split("}", 1)[-1]


def load_strings(prey_files):
    """Localization key (lower-case, no '@') -> English text."""
    strings = {}
    d = os.path.join(prey_files, "Localization", "English_xml")
    if not os.path.isdir(d):
        return strings
    for fn in os.listdir(d):
        if not fn.endswith(".xml"):
            continue
        try:
            root = ET.parse(os.path.join(d, fn)).getroot()
        except ET.ParseError:
            continue
        for row in root.iter():
            if local(row.tag) != "Row":
                continue
            cells = ["".join(c.itertext()).strip() for c in row if local(c.tag) == "Cell"]
            if len(cells) >= 2 and cells[0]:
                strings.setdefault(cells[0].lower(), cells[2] if len(cells) > 2 and cells[2] else cells[1])
    return strings


def build_items(prey_files, strings):
    path = os.path.join(prey_files, "Libs", "EntityArchetypes", "ArkPickups.xml")
    items = []
    for proto in ET.parse(path).getroot().iter():
        if local(proto.tag) != "EntityPrototype":
            continue
        props = next((c for c in proto if local(c.tag) == "Properties"), None)
        if props is None or not proto.get("Class") or not proto.get("ArchetypeId"):
            continue
        sub = {local(c.tag): c for c in props}
        inv = sub.get("Inventory")
        stacks = sub.get("Stacks")
        mod = sub.get("EquipmentMod")
        phys = sub.get("Physics")
        key = (props.get("textDisplayName") or "").lstrip("@").lower()
        name = proto.get("Name")
        item = {
            "archetype": "%s.%s" % (proto.get("Library"), name),
            "class": proto.get("Class"),
            "id": s64(proto.get("ArchetypeId")),
            "name": strings.get(key, "").strip() or name.split(".")[-1],
            "category": name.split(".")[0],
            "w": int(inv.get("iWidth", 1)) if inv is not None else 1,
            "h": int(inv.get("iHeight", 1)) if inv is not None else 1,
            "inventory": inv is not None and inv.get("bAddToInventory") == "1",
            "stackable": stacks is not None and stacks.get("bStackable") == "1",
            "max": int(stacks.get("iMaxCount", 1)) if stacks is not None else 1,
            "mass": float(phys.get("Mass", 1)) if phys is not None else 1.0,
            "deprecated": props.get("bDeprecated") == "1",
        }
        if mod is not None:
            item["chipset"] = "suit" if mod.get("bSuitMod") == "1" else "scope"
        items.append(item)
    items.sort(key=lambda i: i["archetype"])
    return items


def build_abilities(prey_files):
    path = os.path.join(prey_files, "Ark", "Player", "Abilities.xml")
    out = {}
    for a in ET.parse(path).getroot().iter():
        if local(a.tag) == "ArkAbility" and a.get("ID"):
            stats = [[m.get("StatName"), float(m.get("Modifier"))] for m in a.iter() if local(m.tag) == "StatModifier"]
            sig = [[s64(m.get("ModifierId")), m.get("IsInbound") == "true"]
                   for m in a.iter() if local(m.tag) == "SignalModifier"]
            out[str(s64(a.get("ID")))] = {"name": a.get("Name"), "cost": int(a.get("Cost") or 0),
                                          "power": a.get("Power") or "", "level": a.get("PowerLevel") or "",
                                          "stats": stats, "signals": sig}
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    prey_files = sys.argv[1]
    strings = load_strings(prey_files)
    items = build_items(prey_files, strings)
    with open(os.path.join(OUT, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, indent=0, ensure_ascii=False)
    abilities = build_abilities(prey_files)
    with open(os.path.join(OUT, "abilities.json"), "w", encoding="utf-8") as f:
        json.dump(abilities, f, indent=0)
    print("items.json: %d items   abilities.json: %d abilities   (%d strings)" % (
        len(items), len(abilities), len(strings)))


if __name__ == "__main__":
    main()
