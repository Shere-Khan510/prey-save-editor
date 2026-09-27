# Prey (2017) Save Editor

A save editor for Arkane's **Prey (2017)**. Prey saves are encrypted, compressed and checksummed, which is why no editor existed. This one decrypts them, lets you edit them, and writes them back in a form the game accepts.

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

| Tab | What you can do |
|---|---|
| **Player** | Health, max health, psi points, and every player stat (HP pool, psi pool, inventory size, suit/scope chipset slots, move speed, …) |
| **Inventory** | Change the stack count of anything you carry, and **add any non-weapon item**: neuromods, materials, ammo, medkits, food, junk, weapon upgrade kits… (searchable picker with in-game names). Quick buttons set all materials or all ammo at once. |
| **Chipsets** | See every suit and Psychoscope chipset you own, and **add new ones** (install them in-game). |
| **Abilities** | Unlock or remove neuromod abilities. The editor applies the same effects the game does when you buy a perk: stat bonuses (e.g. Suit Modification → inventory size and chipset slots), damage-resistance/weapon modifiers, and psi power levels. **Fix missing perk effects** repairs perks that are marked unlocked but never got their bonuses. |
| **Advanced** | The whole save as a searchable tree. View and edit any value in `save.CSF` or any `*.level` file. |
| **Launch Prey** | Starts the game from the editor (offers to save your edits first). Steam installs are found automatically; for GOG/other installs it asks for `Prey.exe` once and remembers it (change it with the **…** button). |
| **Backups…** | Every save writes a backup first. Load any backup into the editor, or restore it over the slot in one click. |

Tested in-game on the Steam version (build 11720871): edited saves load normally and edits such as 99 neuromods show up in-game.

## Install & run

1. Install [Python 3.8+](https://www.python.org/downloads/). On Windows, tick **"Add python.exe to PATH"**. No extra packages are needed.
2. Download **PreySaveEditor-vX.Y.Z.zip** from the [latest release](https://github.com/Shere-Khan510/prey-save-editor/releases/latest) and unzip it anywhere.
3. Double-click **`Prey Save Editor.pyw`** (or run `python editor.py`).

## Usage

1. **Close Prey first.** The game can overwrite or ignore edits made while it's running.
2. Pick a save from the dropdown (newest first; it shows campaign, slot, location and time) and click **Load**.
3. Make your changes, then click **Save changes**.

Saves live in `%USERPROFILE%\Saved Games\Arkane Studios\Prey\SaveGames\Campaign<N>\<slot>\`.
Player data (inventory, stats, abilities) is in `save.CSF`; each `*.level` file holds the state of one level you've visited.

Backups go to `%USERPROFILE%\Saved Games\Arkane Studios\Prey\SaveEditorBackups\<timestamp>\`.
Even so, **copy your SaveGames folder somewhere safe before your first edit.**

### Tips
* **Add item…** puts new items in the first free inventory slot (or tops up your existing stack). Weapons, quest items and notes can't be added. Find those in-game.
* "Scope chipset slots" come from the **Psychotronics** perks; "suit chipset slots" from **Suit Modification**.

## Compatibility

| | |
|---|---|
| Steam (PC) | ✅ tested |
| GOG / Epic | should work (same file format), untested |
| Xbox / Game Pass PC | ❌ different save container |
| Mooncrash DLC | untested |

## How the save format works

Reverse-engineered from `PreyDll.dll`. `preysave.py` is a standalone library you can use in your own tools.

```
save.CSF / *.level
└─ "CRY3SDK" magic (7 bytes)
└─ RC4-encrypted payload  (key = 256 zero bytes, first 1024 keystream bytes dropped)
   ├─ zlib chunks: [u32 compressed size][u32 uncompressed size][zlib stream] …
   └─ 64-byte XMLCPB header "0XBP" (uncompressed)
        bytes 4..20 = MD5(all chunks + this header with those 16 bytes zeroed)
                      -> the game rejects the save if it doesn't match
```

The decompressed data is CryEngine **XMLCPB** (binary XML):

* **nodes**: `u16` header (tag id: 10 bits; has-attrs: bit 15; child count: bits 12-13, 3 = extended; children-immediately-precede: bit 14), attribute-set id, child location (backwards distances in node-id space; children are written in blocks of 64), then attribute data
* **tables**: tag names, attribute names, string values, and shared *attribute sets* (lists of `u16` = `nameId << 6 | type`)
* **34 compact value types**: plain int/float/vec3/vec4/string/raw, plus space savers such as `POS8`/`NEG16`, constants `0`–`10`/`255`, vectors with constant 0/1/−1 components, and 30 built-in constant strings

The writer reproduces the game's own output **byte for byte**; this was checked on 138 real save and level files.

```python
import preysave
s = preysave.SaveFile(r"...\SaveGames\Campaign0\manual0\save.CSF")
print(preysave.to_xml(s.root)[:2000])        # dump as XML
neuro = next(n for n in s.root.iter() if n.tag == "Extension" and n.get("name") == "ArkNeuroMod")
neuro.set("m_count", 99)
s.save()                                      # re-encrypts and fixes the MD5
```

## Credits & notes

* Item and ability data (`items.json`, `abilities.json`) was generated with `tools/build_catalog.py` from the game's `Libs/EntityArchetypes/ArkPickups.xml`, `Ark/Player/Abilities.xml` and English localization (via the [Chairloader](https://github.com/thelivingdiamond/Chairloader) project's extracted game files).
* Not affiliated with or endorsed by Arkane Studios or Bethesda. Prey is a trademark of ZeniMax Media Inc.
* Use at your own risk and keep backups.

## License

MIT; see [LICENSE](LICENSE).
