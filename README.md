# OoT Dev Tool v0.8.7

A developer panel for the Ocarina of Time remake running in Lichii. It's built to survive Lichii updates: it only uses interfaces that every Lichii build has.

## Install and first run (read this if you downloaded the tool)

**You need**
- Windows, and **Python 3.12 or newer** from python.org, installed with "tcl/tk and IDLE" ticked (it is by default) and "Add python to PATH".
- A Lichii build (any recent one; the tool launches whichever you pick).
- **Your own unpacked copy of the game** (a loose dump): a folder with `exefs\main` and `romfs\...` inside, made with Lichii's "Extract NUP" from your own `.nup`. The tool reads the game's data from it. No game files are included here and none are ever shared.
- Python packages: open a command prompt and run

```
pip install zstandard mmh3 zbic evfl ainb pillow numpy
```

  (`zstandard` is required; the others enable the Ghidra ELF, the catalog's event API, screenshots and code analysis. The Session tab's **Install missing packages** button runs the same line.)

**Folder layout.** The tool expects to live at `<somewhere>\ZELDAOOT\tools\devtool\` with your Lichii build and your dump beside it:

```
ZELDAOOT\
  Lichii Frizti\            (or any folder with lichii.exe; several are fine)
  OoT_dump\updates\131072\  exefs\  romfs\      (your loose dump; the version folder number may differ)
  tools\devtool\           (this)
```

If they are somewhere else, the first start asks for both folders and remembers them (`user\settings.json`; **Change folders** on the Session tab).

**Start it:** double-click `devtool.pyw` (or `pythonw devtool.pyw`). Nothing is built into the game until you click a Build button on the Mod manager or Player tools tabs.

**First start generates two files from your dump; nothing works right until they exist:**
1. `data\catalog_<buildid>.json`: maps, start points, items, events and AI nodes. Built **automatically** on the first start (about a minute; the status line says so). Until it exists the **Warp, Items and Loadout lists are empty**: that is not a bug, wait for "Catalog written" in the status line, or click **Rebuild game catalog** on the Session tab.
2. `names.tsv`: the names of the game's ~15,000 flags. Not built automatically because it takes a few minutes: click **Rebuild flag names** on the Session tab once (a console window opens). Until then the Flag log and World state show flags as hashes (`#1A2B3C4D`).

Both are keyed to your game version (build ID). After a game update, click both buttons again.

**Then:** Session tab -> **Launch Lichii (debug server on)** -> load a save -> **Connect**. The top line should read "Connected: reads + writes". Everything else is described per tab below.


## Tabs

**Session**
- **Lichii build** (v0.5.9; v0.8.6: installed builds only): a dropdown of every folder under `ZELDAOOT` (3 levels deep) that holds `lichii.exe`, newest first: normally `Lichii Frizti` and `Other builds\Lichii Main`. `Desktop\ARCHIVE` is not scanned, so archived builds are not launch choices; a remembered build that sits in ARCHIVE falls back to the default. `ZELDAOOT\Lichii Frizti` is the default and is marked "default"; each entry shows its tag from the Lichii Builds tray app (`[frizti-v3]`, `[build-117]`). The choice is remembered in `user\settings.json` (`lichii_dir`). Launch, the debug server, Connect and the save folder follow the picked build (a portable `user` folder beside the exe is used when there is one, else `%APPDATA%\eden`).
- Also finds the loose dump, the title and build IDs, and your save folder.
- **Launch Lichii (debug server on)**: starts the picked build on the loose dump with `EDEN_NX2_DEBUG_SERVER=127.0.0.1:6545`.
- **Lichii builds** (v0.8.6, read-only): your tray app **Lichii Builds** (`Desktop\lichii-checker\LichiiChecker`) is the only thing that downloads or installs Lichii builds. It keeps Frizti in `ZELDAOOT\Lichii Frizti`, Main in `ZELDAOOT\Other builds\Lichii Main`, and moves old builds to `Desktop\ARCHIVE\Lichii`.
  - The dev tool only reads that app's `state.json` (installed tag, latest tag, update available, last result, last check, per source) every minute and on **Re-read now**, and shows one line per build. A new build reads "NEW BUILD ... Install it from the Lichii Builds tray icon".
  - It never contacts GitHub and has no Download, Unpack or Archive buttons (v0.5.9-v0.8.5 had them; on Nov 7 they put a duplicate build-117 into `ZELDAOOT\Main build-117`, since moved to ARCHIVE). **Release page** opens the release's GitHub page in your browser; **Open Lichii Builds folder** opens the tray app's folder.
  - The dot at the top right: grey = not read yet, green = up to date, orange = new build, blue = the tray app is installing or waiting, red = the tray app reports a problem or `state.json` can't be read (a half-written file keeps the last good read). Click it to jump to this tab.
- **Connect** (once you're in-game): reads go through the debug server. For writes, it finds Lichii's fastmem arena by matching bytes and then uses `WriteProcessMemory`.
- **Self-test this build**: checks every channel, including screenshots, and writes `user\selftest_<build>.txt`. Run it on each new Lichii build.
- **Setup** row:
  - **Install missing packages**
  - **Rebuild flag names**: the full romfs scan, plus the name lists in `data\name_lists\` and any `main` strings in `user\dump\`
  - **Change folders**: set where Lichii and the unpacked game are, for a friend's PC
  - **Open save folder** and **Open user folder**
- **Screenshot** (top right, any tab): saves the next frame to `user\shots\<time>_manual.png` and previews it on the Session tab.
- **Research tools (v0.4)**:
  - **GameData probe**: finds where the game keeps its flags in RAM, so later versions can edit them live.
    1. With the game **and Lichii** closed, pick a slot and click **Prepare probe save**. (It refuses while Lichii runs: the game writes its slots when you quit, which would save over the prepared values.)
       - The slot is backed up to `user\backups\`.
       - Unique values go into 16 records (minigame records, enemy kill counters and boss-rush times), and the HUD values are set to 17 Deku seeds and 77 rupees, so you can see at a glance that the prepared slot loaded.
    2. Launch, load that slot (check 17 seeds / 77 rupees), wait until you can move Link, then click **Connect** and **Run v0.4 probes**.
       - The probe looks for the markers. If they are missing (slot saved over), it falls back to finding the value table by its contents, using the copy of the save the game keeps in RAM.
       - It then does one live write: the seed count drops by 3 for about 2 seconds, with a screenshot before it is put back.
       - This takes a few minutes and writes `user\probe_v04.txt`, `user\probe_gamedata.txt`/`.json`, screenshots in `user\shots\` and raw memory windows in `user\dump\`.
       - Afterwards it puts your original values back (compare-and-write).
  - **Ghidra ELF of main (offline)**: decompresses `exefs\main` (ZBIC) and applies its 536,000 relocations. It writes `user\dump\main_E0F3CCFAF50065AE.elf`, ready for Ghidra (base `0x7100000000`, so Ghidra address = `0x7100000000` + main offset). It also writes all 68,000 code strings to `main_..._strings.txt`.
  - **Dump main from RAM (in-game)**: copies the loaded `main` (all `0x4EA0000` bytes) to `user\dump\main_..._ram.bin`.
  - **Rebuild game catalog**: rebuilds `data\catalog_<build>.json` from your dump. It holds 80 maps with 415 start positions, 395 items, 2,380 events, 5,531 AI node types and the event-flow API. Later menus (warp, give item, event player) read from it.

- **Test command (Core: +1 rupee)** (v0.6; the hook half confirmed Nov 7, the +1 rupee still unconfirmed): with DevTool Core built and the game booted from the loose dump, Connect, then click it. The tool writes `DevTool.Arg0 = 0`, `DevTool.Cmd = 1` and waits for `DevTool.Ack`. One message per outcome: *Ack seen* (the Sequence hook and the event both ran: expect +1 rupee), *The Sequence hook ran (Ack 100) but the DevTool event never answered* (the hook requested the event; the event did not start), *Cmd cleared without an Ack*, *Ack never seen* (hook did not run), or *DevTool.Cmd not in the running game's GameData* (the GameDataList part did not load). Seen Nov 7 with v0.8.2 and again with v0.8.3 (sorted EventSetting row): Ack 100 within the same second, then no event. Cause not confirmed yet; see the Mod manager's *event* part.

**Status line** (under the top bar, once connected): current map, child/adult, in-game day and time, hearts, rupees and Link's position, read live from the game twice a second.

**World state** (save editor and live editor)
- **Save file**: every GameData value in a save slot, ~10,000 flags plus items, counters, enums, strings, vectors and arrays.
- **Live game** (v0.5, after **Connect**): the same list read from the running game, about 13,000 values, refreshed twice a second.
  - Double-click a value to change it in the game right away. This works for Bool, Int, UInt, Float, Int64, UInt64, Enum, Vector3 and text values. There's no Save button: the change is live, and the game keeps it when it next saves.
  - The game still enforces its own limits on its next change. Example: 999999999 rupees showed in the menu, then buying a potion capped them to the wallet size (200). Freeze the value to keep it.
  - **Freeze / unfreeze selected** holds a value: whenever the game changes it, the tool writes it back within half a second. Frozen rows show in light blue. Freezes end when you reconnect.
  - **Snapshot** then **Diff vs snapshot** (or the "changed since snapshot" filter) shows what an action changed, without waiting for the game to save.
  - Arrays and Binary values are only shown from the save file for now.
- Search by name or `#HASH`. Filters: type, named only, not default, changed since snapshot.
- Double-click a row to edit it. **Save changes** writes the file in place, after backing up the original to `user\backups\`.
- Flag discovery: **Snapshot**, then play until the game saves, then **Reload** and **Diff vs snapshot**. You'll see exactly which flags and values that action changed.
- **Slot snapshots...** (v0.5.3) keeps named copies of a slot folder (`progress.sav` + `caption.sav`) in `user\slot_snapshots\`.
  - **Restore into the slot** puts one back, after backing up the slot to `user\backups\`. Do it with the game closed or at the title screen.
  - **Compare with the slot** lists every value that differs between the snapshot and the slot.
- **Label hash** attaches your own note to a hash. **Try names** hashes a pasted list of candidate names and keeps any that match. Both save to `user\names_user.tsv`.

**Flag log** (v0.5)
- Lists every GameData value the game changes while you play: time, name, type, old value, new value.
- Starts on its own once you connect. Each session also goes to `user\flaglog\flaglog_<time>.tsv`.
- **Mark** puts a line in the log. Press it before and after an action to bracket exactly what that action changed.
- Your own changes (edits and freezes) show in light blue, marked "(set by you)" or "(freeze put it back)", so they aren't mistaken for the game.
- The in-game clock, Link's position and the play-time counters are muted from the start. Any value that changes 6 times in 10 seconds, or 10 times in a minute, is muted too. **Mute selected** mutes the rows you pick; **Unmute all** brings everything back.
- **Show only** filters the list by name. Double-click a row to open that value in World state (live).

**Warp** (v0.5.1, experimental)
- Pick a map (80, from the game catalog) and a start point, then click **Arm warp** and walk through any door, exit or loading zone.
- How it works: each door writes the next map and start point into GameData (`Sequence_ReservedNextBanc`, `Sequence_ReservedNextStartPosName`, plus an arrival event) just before the game loads it. While armed, the tool overwrites those with your choice about every millisecond.
- It disarms as soon as the current map changes (or after 5 minutes), reports whether your map or the door's map loaded, and takes a screenshot once the new map is on screen.
- Tested Nov 7: Market → Kakariko, Bottom of the Well → Desert Colossus, Desert Colossus → Market, all three on target.
- A map of the other age switches Link's age on arrival, because `Sequence_IsChildhood` follows the map. Untick "hide the other age's maps" to use it as an age swap.
- "Skip the arrival cutscene" clears the door's arrival event, which belongs to the door's own target. The map list hides the other age's maps by default.

- **Farore's Wind point** (v0.5.3): the spell's saved point holds a map, an exact position and a facing (`WarpPoint_FaroreWind.*`).
  - Shows the current point, and whether the save has the spell.
  - **Set to Link's position**, **Set to the start point picked above**, **Clear**, and **Give Farore's Wind** (sets `IsGet.Item_FaroreWind` and the pouch count).
  - Then cast the spell and choose Return: Link lands at the point, on any map. The spell needs magic (Player tab).
  - Confirmed in-game Nov 7: a cast in Hyrule Field with the point set to Market_Child landed exactly there. The game does it through `Sequence_IsWarpByFaroreWind`, `Sequence_ReservedNextBanc`, `Sequence_ReservedNextPosition` (a raw position, no start point name), `Sequence_ReservedNextCancelSleepRot` (facing) and `Sequence_ReservedBootEventName = FaroreWindArrive`, with a white fade.
- **Position bookmarks** (v0.5.3): save Link's map and position under a name (`user\bookmarks_<build>.json`), then **Put in Farore's Wind** to go back there.

**Player** (v0.5.3)
- Rupees, hearts, max hearts, magic, max magic, heart pieces, and the counts of sticks, nuts, seeds, arrows, bombs, bombchus and magic beans, live from the game.
- Type a value and press Enter or **Set**. **Freeze** keeps it there. **Full hearts** and **Full magic** fill to the maximum.
- Hearts count in quarters (4 = one heart), as the game stores them.
- **Counts make the item appear** (v0.8.1 fix, found in-game Nov 7): the pouch lists an item only when `IsGet.<item>` is True. Bombs set to 30 stayed invisible without it. Now any count above 0 the tool writes (Player tab, World state, freezes, Loadout; live or save) also sets `IsGet.<item>` True; a count of 0 leaves it alone.
- **Magic and hearts** (v0.5.5; both confirmed in-game Nov 7: magic held at 48/96/200, hearts 4 showed one heart after a door): during play, the game overwrites Magic every frame, so a write snaps straight back.
  - The working theory is that Link keeps his own copy and reads GameData only when a map loads.
  - So the tool holds the value through the next loading screen: go through any door. The Player tab shows what is waiting.
  - After the load, the tool watches the value for 30 seconds. It logs either that it held, or exactly when the game changed it and to what.
- If any value goes straight back after **Set**, the tool says so.
- If a frozen value keeps getting reset, the tool says so. The flag log then shows the freeze once per 10 seconds instead of every rewrite.
- **Give magic meter** / **Give double magic** (v0.5.4): the meter is the Great Fairy's blessing.
  - Sets `IsGet.Item_FairyBless_MP` (and `..._MPDouble`) and the pouch counts.
  - If the game doesn't fill in Max magic itself within 1.5 s, it sets Max magic to **240** (single) or **480** (double).
  - Then it holds Magic at that maximum for the next loading screen: go through any door.
- **Magic units** (v0.5.7, from the dump; confirmed in-game Nov 7): one meter is 240, double is 480.
  - `RSDB\PouchActorInfo`: `Item_FairyBless_MP.MPMax = 24`, `Item_FairyBless_MPDouble.MPMax = 48`; magic jars `MPRecover` 60 (small) and 120 (large); `Obj_FairySpirit.MPRecover = 480`.
  - The item-get code (`main+0x11103C8`, for items whose `ExpansionAvility` is `MPMaxUP`) multiplies the table's `MPMax` by 10 and writes it to `PlayerStatus.MPMax`, then fills `PlayerStatus.MP` to it.
  - That makes a small jar a quarter meter and a large one a half, the classic ratios (12 and 24 of 48). The earlier 48/96 were classic-OoT guesses.
  - Confirmed in-game (v0.5.8): the bar is 8 segments at 240 and 16 at 480; Magic 60 fills exactly 2 segments; a Farore's Wind cast costs 60 (`PlayerStatus.MP` 480 → 420).

**World** (v0.7, unconfirmed; every action has a *raw* route and a *Core* route, picked at the top)
- **Map select**: all 415 start points (map / point; "(default)" is the map's unnamed start). *Start point* mode writes what a door writes (`Sequence_ReservedNextBanc`, `...NextStartPosName`, empty boot event, `LoadingFade`); *coordinates* mode writes what Farore's Wind writes (`Sequence_ReservedNextPosition`, `...NextCancelSleepRot`, `Sequence_IsWarpByFaroreWind`, boot event `FaroreWindArrive`, `WhiteFade`). Both then set `Sequence_IsLoadingBanc = True`. Whether that alone starts a load is the open question (the v0.5.1 warp needed a door); if it does nothing, **Arm warp** + a door still works, and the Core route is `GameSystemActor::EventReserveBancChange` with the start point baked in (one case per start point in the DevTool flow, `Cmd 10`, `Arg0` = index).
- **Reload this map here** (position route with Link's current position), **Respawn at last start point** (`Sequence_ReservedNextStartPosName` as last set by a door, else the map's first point), **Void out** (position route with `PlayerStatus.SavePos`). Core route: a reload at the map's first listed start point.
- **Swap age in place**: flips `Sequence_IsChildhood` and reloads here (the age followed the map on load in the v0.5.1 test).
- **Time**: raw = `World_CurrentDateTime` (day kept, seconds of the day replaced; presets morning/noon/dusk/night or hour:min). Core = `EventWorldManagerController::EventTrigger_SetTime(hour, 0)` (`Cmd 13`, `Arg0` = hour). **Weather** has no known raw value (`World_WeatherForecast` is an IntArray): Core only, `Event_SetWeatherForFlowchart` with the two names the game's own flows use (`GanonCloudy`, `Thunderstorm`) or `Event_ResetWeatherForFlowchart` (`Cmd 14`).
- **HUD**: Core only, `UISystemActor::EventTriggerSetDisplayHeartGauge/MiniMap/SmallKey/BossKey` (`Cmd 15`, `Arg0` = bit mask of hidden parts). **This dungeon**: `EventGetDungeonSmallKey/BossKey/Map/Compass` for the dungeon you are in (`Cmd 16`).
- **Event player**: Core, `Cmd 17`, `Arg0` = index into the 2,037 EventSetting rows that have a flowchart and an entry point, each a sub-flow call inside the DevTool event. It is only generated when "event player" is ticked in the Mod manager before building Core (2,000 sub-flow references may make the event system load every flowchart; turn it on once the hello command works).
- **BGM / SE / Message**: Core, `EventTriggerStartBgmSimple` (50 BGM names seen in the flows, `Cmd 18`), `EventSoundStartSound` (90 SLink keys, `Cmd 19`), `SystemTextNPC::EventTalk` (47 message IDs the system NPC shows, `Cmd 20`), `EventTriggerStopInGameBgm` (`Cmd 21`). No action takes a free string, so the lists are what the flows use; playing a BGM by any other name would need a new flow case.

**Items** (v0.7, unconfirmed)
- **Give item**: every `PouchActorInfo` row with a filter. Raw = `IsGet.<item> = True` and `PouchData.<item>.StockNum += count` (the route Farore's Wind and the magic meter already use). Core silent = `PouchSystemActor::EventTriggerAddOrRemovePouchContent(Actor, Num 1, IsGetEvent False)` (`Cmd 12`), Core with scene = the same with `IsGetEvent True` (`Cmd 11`); `Arg0` = index into the item list. Without a connection the raw writes go into the loaded save slot (then **Save changes** on World state).
- **Bottles** (v0.8.3 fix): writes `PlayerStatus.AssignableItem.BottleInfo.<Bottle>.BottleItem` (an Enum in the GameDataList: `Empty`, `RedPotion`, ... `Fairy`, `HalfMilk`; v0.8.2 wrote item names like `Item_Bottle_Fairy`, which are not values of it), `IsValid = True`, and `IsGet.Item_Bottle` if you had no bottle. The same writes as the Loadout tab. Unconfirmed in-game: that the bottle then shows the content.
- **Songs, equipment, flags**: `IsGet.*` toggles for the 13 songs, swords, shields, tunics, boots, gauntlets, bow, slingshot, Farore's Wind and the magic meters; `IsLastSelectedMQMode` (the only MQ-related GameData name; `NewGameMQ`/`ReleaseMQ` are code strings, not flags: unconfirmed what it does on a running game); the amiibo switches (`IsEnableUseAmiibo`, `IsEnableUseAmiiboField`, `OptionData.IsUseAmiibo`, `IsGet.Pouch_Amiibo`). **Read current values** fills the boxes from the game or the save.
- **Dungeons**: `DungeonInfo.<dungeon>.SmallKeyNum / IsGetBossKey / IsGetMap / IsGetCompass` per dungeon, `HaveDefeatedEnemy_Enemy_DungeonBoss_<boss>`, and the stones and medallions (`IsGet.Item_Stone_* / Item_Medal_*` plus their pouch count).
- **Quest flags**: a read-only tree of the main-quest step and completion flags (names with `_IsCompleted`, `_IsAfter_`, `_Step`, `DoorOfTime_`, `AllSage_`, the medallion/stone and boss flags), grouped by prefix, with live values (or the save's).

**Player tools** (v0.8, unconfirmed)
- **DevTool Player mod** (built here, game closed; its RESTBL sizes go into the merged table): the player bridge inside `AI\Player.scheduler.module.ainb` of `Pack\Actor\Player_Base / _Child / _Adult.pack.zs` (identical copies in the dump; all three are rewritten), plus same-size float patches of `Player\PlayerGameParam\Player.game__player__PlayerGameParam.bgyml`. Six bridge parts, each a switch so a boot crash can be bisected:
  - *pos*: `QueryActorGetPosition` -> `OneShotSetNonStructGameDataVector3("DevTool.PlayerPos")` every frame.
  - *setpos*: an `Element_S32Selector` on `QueryGetNonStructGameDataInt("DevTool.Cmd")`: case 30 runs `ExecuteActorForceSetPos(QueryGetGameDataVector3(struct DevTool, "Pos"))` together with `DevTool.Ack = 1` and `DevTool.Cmd = 0`. The Sequence hook ignores Cmd 30-39, so the DevTool event is not started for bridge commands.
  - *moonjump*: `Element_BoolSelector` on `DevTool.MoonJump` -> `OneShotPhysicsSetLinearVelocity((0, 12, 0))` every frame while true.
  - *gravity*: `OneShotPhysicsCctSetGravityScale(DevTool.F0)` every frame (1.0 = normal).
  - *god*: `OneShotLifeSetInvincible(DevTool.God)` every frame.
  - *debugmove*: the scheduler's secondary (debug) root is wrapped in a `BoolSelector` on `DevTool.DebugMove`: true picks the existing `DebugMove` branch (`ExecuteBSASetActionVerb` 62), false the original selector. Retail may stub DebugMove; this just makes the branch reachable.
  - The bridge nodes hang under a new `Element_Simultaneous` that becomes the main root with the vanilla root (`SelectorPlayerSchedulerReaction`) as its last child. Node templates come from the game's own AI files (`Enemy_DungeonBoss_Morpha`, `NpcCommon`, `Kokko`, `Player.event.root`...). Every added node is listed in `docs\research\player_bridge_nodes.json` after a build.
  - *Speed x*: `GroundSpeedScale` x N (C4 first version, no markers yet). *No fall damage*: `LargeLandHeight` 5 -> 100000 (the landing-height threshold; unconfirmed that it is the fall-damage one).
- **Bridge toggles**: god mode, debug move, moon jump (hold the button, or latch), gravity scale: plain writes of `DevTool.*`, so they need the Core GameDataList part loaded. **Infinite magic and ammo**: one checkbox that freezes `PlayerStatus.MP` at `MPMax` and the stick/nut/seed/arrow (`Item_Arrow_Normal`; v0.8.2 froze a non-existent `Item_Arrow`)/bomb/bombchu counts at their current values (Link overwrites Magic every frame during play, so that part may only hold after the next loading screen, as with the magic meter).
- **Teleport** (B2), three routes: (a) the raw position map change (World tab's route, current map, costs a load), (b) the bridge: `DevTool.Pos` + `Cmd 30`, instant, same map, Ack from the player graph, (c) the Farore's Wind point (then cast). **Copy my position** fills X/Y/Z. The **bookmarks** are the Warp tab's (name, map, position); "Go to the selected bookmark" uses the chosen route (the bridge route refuses another map).
- **Save states**: **Snapshot state** copies the slot's `.sav` files into `user\states\<time>_<name>\` with the map, position, facing and clock (`state.json`); **Restore state** copies them back into the slot (backup in `user\backups\`), writes the clock, and when connected reloads the map at the saved position through the raw position route. The game reads the slot files only when you load from the title screen, so the files matter for the next load; the position reload is what you see at once. Up to 10.
- **Info bar**: FPS from the debug server's `status` every 2 s. Actor count: nothing exposes one (debug server or GameData), so it is not shown. **Split timer**: F9 start / split, F10 reset, on the tool window (not in-game); splits are logged.
- **Overlay test** (E4): "Show a text box" = Core message 0 (`SystemTextNPC::EventTalk`). The event API has no free-text action, so an overlay with the tool's own text needs the code side (v1.2).

**Live memory**
- Read and hex-dump any guest address (`0x...` or `main+0x...`).
- Write u8/u16/u32/i32/f32/u64/f64/hex/string values.
- Heap scan with "next scan" refine, the Cheat Engine workflow. The **flag name/hash** scan type finds where a GameData hash sits in RAM.
- Watches, with freeze, saved per game build in `user\watches_<buildid>.json`.

**Camera**
- FOV sliders for each camera mode: exploring, Z-targeting, aiming, riding Epona, and talk/event cameras. The range is 25–250%, capped at 150°.
- Camera distance (zoom in and out) runs from 20% to 500%. Right-stick sensitivity runs from 25% to 400% and applies to all 5 in-game sensitivity levels.
- **Build camera mod** writes a RomFS mod to `%APPDATA%\eden\load\0400486024DEC000\DevTool Camera\`.
  - It only changes float values inside `PlayerCamera.pack.zs`, so the file size stays the same.
  - Every value also gets a tiny unique marker (a change of less than 0.8%), so the live tool can find the game's own working copies in RAM.
  - Close the game before building, because Lichii locks the file while it runs.
- **Live control (no restart):** get in-game, click **Connect**, then **Find live camera**. Tick **Apply slider changes live**, then move the sliders.
  - The tool finds the camera parameter files plus the game's parsed working copies by their markers. It only accepts markers that sit together in a cluster.
  - It writes with compare-and-write, so it never touches memory that has since been reused.
  - Everything it found is saved in `user\camera_probe.txt`.
  - Live changes last until the game restarts. Build the mod to keep them.
- **Check mod loaded** tells you whether the game loaded the camera mod. It needs a fresh boot with no live edits.

**Mod manager** (v0.6; Core boot confirmed in-game Nov 7, the rest unconfirmed)
- Lists every folder under `%APPDATA%\eden\load\0400486024DEC000\` (or the portable `user\load\...`), in the order Lichii layers them, with its file count, whether it ships a RESTBL, and a note (tool-built, code patches, CONFLICT).
- **How Lichii loads RomFS mods** (read from its source, `src/core/file_sys/patch_manager.cpp` `ApplyLayeredFS`, same in zortos293 `9fa4a71` and Frizti `72e5ad6`): the mod folders are sorted by name in byte order, each enabled folder's `romfs\` becomes a layer in that order, vanilla is the last layer, and a file comes from the **first** layer that has it (`vfs_layered.cpp`, `LayeredVfsDirectory::GetFileRelative`). So "0 DevTool RESTBL" beats "DevTool Core" beats "DevTool Fog" beats "Perf - Balanced".
- **Enable / disable selected**: Lichii's own mechanism, the `[DisabledAddOns]` list in `config\qt-config.ini` (what the Add-Ons tab edits). Lichii rewrites that file when it closes, so the button refuses while Lichii runs. Toggling also rebuilds the merged RESTBL.
- **Check conflicts**: the same romfs path in two enabled mods (first wins, named), and more than one enabled RESTBL.
- **Rebuild merged RESTBL**: writes one table into `0 DevTool RESTBL\romfs\System\Resource\...rsizetable.zs`: vanilla, plus every entry an enabled mod's RESTBL changed (first mod wins per entry, matching the file the game will load), plus the sizes the Core files need (grown entries for the GameDataList, MainUpdate and EventSetting; new entries for `DevTool.module.ainb` and `DevTool.bfevfl`, with an unconfirmed 25% + 4 KiB margin).
- **What loaded? (Lichii log)**: Lichii logs only "RomFS: LayeredFS patches applied successfully" (no per-mod line; the IPS/pchtxt patch names are logged, RomFS folders are not). The tool reports that line plus which folders were enabled, which is the best it can do without a Lichii change.
- **Build perf mod**: runs `tools\perfmod\make_perfmod.py` with the chosen preset straight into the load folder (`Perf - <preset>`). Fog is built in the tool (**Fog**, below) and the camera mod is built from the Camera tab. Close the game before any of these builds.
- **DevTool Core** (Engine 2): generated from your dump into `DevTool Core\romfs\`. Three parts, each a checkbox, so a boot crash can be bisected by rebuilding with one part off at a time:
  - *gamedata*: `GameData\GameDataList.Product.110.byml.zs` with 17 new non-saved entries (`DevTool.Cmd`, `Arg0..3`, `Ack`, `F0..3`, `Str`, `Pos`, `PlayerPos`, `DebugMove`, `God`, `MoonJump`, `HideHud`; SaveFileIndex -1, reset type 0; markers in Arg1..3) and a `DevTool` struct listing them. The save file format does not change (nothing is saved). When this part is built and enabled, the tool loads its GameData definitions from the mod's list, so the live tables line up (15,015 definitions instead of 14,997) and `DevTool.*` shows in World state and the flag log.
  - *sequence*: `Sequence\DevTool.module.ainb` (v0.8.2, shaped like the game's `NaviHintEvent.module`: a selector on `DevTool.Ack` (100 = already requested -> End), then a selector on `DevTool.Cmd` (0 and 30-39 -> End; anything else = `SeqOneShotRequestEvent("DevTool")` together with `Ack = 100`); every branch ends in a one-shot, because the v0.8.1 module parked on `SeqExecuteGenericDummyAction`, which never ends, and never re-checked Cmd. Confirmed in-game Nov 7: Ack goes to 100 within a second of the Test command) and `Sequence\MainUpdate.module.ainb` with that module added as the 6th child of the root `Element_Simultaneous`. Written with the `ainb` package (0.2.16 needs an empty blackboard object or it writes a file it can't read back; the vanilla MainUpdate round-trips byte-identically).
  - *event*: `Event\EventFlow\DevTool.bfevfl.zs` (entry `Run`: `GameDataActor::EventQueryGameDataIntBranch(DevTool.Cmd)`; case 1 = `PouchSystemActor::EventTriggerIncreaseRupee(Value=1)` then `EventTriggerSetGameDataInt(DevTool.Ack, 1)` then `DevTool.Cmd = 0`; cases 2-15 = `GameSystemActor::EventTriggerSendErrorToEventResource` then clear) and `RSDB\EventSetting.Product.111.rstbl.byml.zs` with a `DevTool` row copied from `NaviLockOnHint` (flowchart `Work/Event/EventFlow/DevTool.evfc`, component list `NoPauseEventWithIgnoreDissentPlay`, an existing file, so the event does not pause the world). **v0.8.3:** the row is inserted in its sorted place (the vanilla table is sorted by `__RowId` in byte order, all 2,379 rows; `DevTool` lands at row 263 of 2,381) and the build checks the order. v0.8.2 appended it at the end. Sorting it was not enough on its own: the 17:16 hello with the sorted row still gave Ack 100 and no event. The row stays sorted because every vanilla RSDB table here is.
  - **GameAnalyzedEventInfo row** (v0.8.5, part of *event*): `RSDB\GameAnalyzedEventInfo.Product.111.rstbl.byml.zs` gets a `DevTool` row, copied from `NaviLockOnHint`'s all-default row (no recorded camera, no terrain centre, no sub-flows) and inserted in sorted order. Every vanilla EventSetting row that has a flowchart has a row there (2,379 of 2,380; the one without, `PlayOcarina_ReplaceEvent`, has no flowchart). Guess: the event system looks events up there before starting them. Unconfirmed until the next hello.
  - *Hello only* (v0.8.3 bisect switch): builds a DevTool flow with only the +1 rupee case (about 700 bytes instead of 705 KB, larger than any vanilla flow, which tops out at 318 KB). Every other Core command is off in that build. Use it only if the hello command still fails with the full flow, to tell flow size apart from the row/component list.
  - *Fallback: reuse existing entries*: builds the sequence and event parts on `Npc_TimeSignal.Hour/Minute/Second/HourAMPM` (non-saved Ints only the time-signal NPC writes) instead of new entries, for the case where the game rejects a longer GameDataList. Unconfirmed; the morning test decides.
  - Every generated file is re-read with `byml` / `ainb` / `evfl` before the build counts (8 checks, in `DevTool Core\devtool_core.json` with the file list and RESTBL sizes). **Remove DevTool Core** deletes the folder: that is the one-step undo.
- Confirmed in-game Nov 7: the game boots with all three Core parts, accepts the longer GameDataList (15,015 definitions, `DevTool.Cmd` writable live), loads the merged RESTBL with grown entries, and runs the added Sequence module (Ack 100). The zstd rule that made it boot: every file keeps its vanilla dictionary, and RSDB tables, the RESTBL, `UI\*.bntx.zs` and `ZsDic.pack.zs` use none.
- Still unconfirmed: whether `SeqOneShotRequestEvent` starts the DevTool event (v0.8.2 and v0.8.3 boots: Ack 100, no event; the v0.8.5 GameAnalyzedEventInfo row is the next try, then *hello only*), and the RESTBL margin for new files (25% + 4 KiB; vanilla flows get their size + about 280 bytes, so it is generous).

**Fog** (v0.8.4, in the Mod manager tab, `fog.py`; unconfirmed in-game)
- The fog mod is part of the tool now. One folder, **DevTool Fog**, replaces the four old `Fog - *` folders. The first row under "Build the tool's own mods" has the presets **Lichii fix / Light / Half / None**, **Custom**, **Build fog mod**, and **Sliders + live >**. That button opens the four sliders and the live controls. On the tool's 1320x800 window the open sliders push the Core controls down, so hide them again when you need Core.
- Presets (x1 = the console look):

  | Preset | Volumetric density | Distance haze | Glow | Mie | Also |
  |---|---|---|---|---|---|
  | Lichii fix | x1 | x1 | x0.3 | x1 | nothing else |
  | Light | x0.5 | x0.75 | x0.3 | x0.85 | fog volumes x0.5, linear fog x0.6, cutscene fog x0.6 |
  | Half | x0.2 | x0.5 | x0.3 | x0.7 | fog volumes x0.25, linear x0.4, cutscene x0.35 |
  | None | off | x0.01 | (unused) | x0.5 | fog volumes off, linear fog 0, cutscene fog x0.01 |

  Glow x0.3 is the Lichii fix: Lichii draws volumetric fog 3-4x too bright (Lichii PR #87). Once Lichii fixes that, use Custom with glow x1.
- **Custom**: four sliders, 0-2x each: volumetric density, distance haze, glow and mie. The other three factors are derived: local fog volumes follow density, and linear and cutscene fog get the mean of density and haze. That rule is a guess that matches Light and Half. Density 0 switches volumetric fog off, as None does.
- What it edits (the decoded facts are comments at the top of `fog.py`):
  - `Pack\EnvPalette.pack.zs`: `VolumeFogParams1/2` Near/FarDensity, `VolumeFogIsEnabled`, `VolumeFog*LightScale`, `SfParam_density`, `FogColor.A` and `SkyRParam_mie_amplifier`, plus the indoor `VF*` keys.
  - `Pack\DemoPalette.pack.zs`: `FogDensity`, `YFogDensity`, `VolumeFogIntensity`.
  - `agl\SkyParam`: `ScatterFogDensity`.
  - `System\GameGraphics\VolumeFogAreaParam`: the 188 local fog volumes.
- Left alone unless **--all** is ticked: scene-transition fades, lava and damage volumes, and the Haunted Wasteland sandstorm. Underwater fog is never touched.
- **Build fog mod** (game closed):
  - builds in the tool's cache, then swaps the finished folder into the load folder as `DevTool Fog`
  - enables `DevTool Fog`, disables any old `Fog - *` folder that is still on, and rebuilds the merged RESTBL
  - writes no RESTBL of its own: the entries that grew go into `devtool_fog.json` and from there into "0 DevTool RESTBL" (73 entries for Lichii fix, 92 for Light and Half, 222 for None on this dump)
  - writes `fogmod_report.txt`, which lists every value changed
  - on the first build, asks once whether to move the old `Fog - *` folders into `user\backups\fog_old\`
- With the live markers off, every preset builds byte-for-byte the same files as the old `tools\fogmod` builder, and its RESTBL entries match the old fog RESTBL's (checked Nov 7 against the four installed folders).
- When the tool starts, the tab selects the fog folder that is enabled in Lichii's Add-Ons (`[DisabledAddOns]` in `qt-config.ini`). If none is enabled, no preset is selected and the line under the sliders says "no fog folder enabled (vanilla fog)" (v0.8.5 selected the None preset there, which is the opposite: no fog).
- **Vanilla** (v0.8.6, next to **Build fog mod**; unconfirmed in-game): back to the game's own fog.
  - Game and Lichii closed: disables `DevTool Fog` and every old `Fog - *` folder in Lichii's Add-Ons list, then rebuilds the merged RESTBL so their entries drop out. Nothing is deleted: **Build fog mod** turns it back on. Boot the loose dump to see vanilla fog.
  - In-game with live fog found: writes the vanilla value (x1 everywhere, glow x1) to every live copy, until the next boot. Unconfirmed, like all live fog. The folder stays on for the next boot until you click Vanilla again with the game closed.
  - In-game without a live target: it refuses (Lichii rewrites its add-on list when it closes).
- **Live fog** (unconfirmed):
  - **Mark values for live fog** (on by default) gives every density, haze, glow and mie float in `EnvPalette.pack.zs` a 16-bit id in its lowest mantissa bits. That is the Camera tab's method, and it changes each value by less than 0.8%.
  - After **Connect**, in the world, **Find live fog** looks for the unpacked pack in RAM by its SARC header (checked against marker values) and for other copies: clusters of marked values, which may be the parsed palettes.
  - **Apply slider changes live** writes vanilla value x slider (still marked) to those addresses. It uses compare-and-write, so memory that has been reused is left alone.
  - The search can come back **NOT FOUND**. Then the game keeps no marked palette value where the tool can see it, live fog is not available this way, and the build-time presets are what works. Details are in `user\fog_probe.txt`.
  - Even when the search finds copies, it is unconfirmed that the game re-reads them after load.
  - In a None build, volumetric fog is switched off, so the density slider does nothing live. Build Lichii fix or Custom to try live density.
- Unconfirmed assumptions:
  - 57 palettes never set `SfParam_density`. The tool assumes the engine default is 0.85 (from `agl\SkyParam`), which only matters when haze is not x1.
  - Hyrule Field, night and rain have never been checked in-game with any preset.

**Cheats**
- Edits `%APPDATA%\eden\load\0400486024DEC000\DevTool\cheats\<buildid16>.txt`, which uses the Atmosphère format. Restart the game after saving.

**Loadout** (v0.8.1, unconfirmed in-game; `loadout.py`)
- One screen that mirrors Link's pouch: **Items** (slingshot, bombs, bombchus, boomerang, hookshot/longshot, bow, arrows and the three magic arrows, Din's / Farore's / Nayru's, lens, beans, hammer, sticks, nuts, ocarina, bottles 1-4 with contents), **Equipment** (swords, shields, tunics, boots, each with an **Equip** button), **Upgrades** (bomb bag, quiver, bullet bag, stick and nut capacity, wallet, scale, strength, magic meter, double defense), **Quest** (stones, medallions, songs, Gold Skulltula tokens, Gerudo's Card, Stone of Agony, heart pieces, heart containers, masks, trade items) and the **Equipped / assigned** slots (`PlayerStatus.Equip*`, `PlayerStatus.AssignableItem.Child/Adult`).
- Each row shows the live state ("have" / "don't", count / cap, the size owned) and has **Give** and **Take**; counts and sizes have a picker. Without a connection it edits the save loaded on World state (then **Save changes** there).
- Raw route: `IsGet.<item>` + `PouchData.<item>.StockNum` (1 for key items, the count for ammo) + the slot values. Sizes (bags, hookshot, ocarina, scale, strength, magic) give every smaller size too (a guess: nothing in the game seems to clear an `IsGet`).
- **Ammo caps** (found Nov 7 in saves, unconfirmed live): the current maximum per `MaxNumGameData` key lives in an unnamed GameData struct `#F80F8077` with the members `Rupee`, `DekuSeed`, `Bomb`, `Arrow`, `DekuStick`, `DekuNut`. In every save checked (7) its values equal `AfterExpansionMaxNum` of the bag owned (wallet 200, bullet bag 30, stick bag 10, nut bag 20; 0 with no bag). Giving a bag sets it (sizes read from your dump's `RSDB\PouchActorInfo`) and, with **bags fill their ammo**, fills the ammo the way `IsItemStockMaxIfGet` says (bombchus only if you already have them). Giving bombs with no bag gives the small bag first.
- Unconfirmed guesses, marked in the tab: bombchus share the bomb cap (`MaxNumGameData Bomb`), the Kokiri Tunic is `Armor_Adult_Normal`, `Weapon_Sword_Giant` is the Giant's Knife, `DiveDepth` 3 / 6 / 9 for no / silver / gold scale, `LiftGrade` Low / Middle / High for bracelet / silver / gold, `IsDefenseUP` with double defense, Equip slots taking effect before the next load. Magic 240 / 480 is confirmed (v0.5.8).
- Bottles: `PlayerStatus.AssignableItem.BottleInfo.BottleA-D` are `IsValid` (have the bottle) + `BottleItem`, an Enum (`Empty`, `RedPotion`, `GreenPotion`, `BluePotion`, `BlueFire`, `Fish`, `Letter`, `BigPoe`, `Poe`, `Bug`, `Fairy`, `MaxMilk`, `HalfMilk`). There is a fifth slot, `BottleE`, not shown.
- **Presets**: Child start, All child items, All adult items, Everything (replace mode = Strip first, add mode = on top), Strip. They live in `data\loadouts.json` (row ids and value forms are explained at the top of the file); **Reload presets** re-reads it. Presets always use the raw route.
- **with the "you got" scene (Core)**: gives through the Core give-item command (one item with the game's own scene), then sets counts raw. Disabled until the Core give-item command is confirmed in-game (the Core hello command does not work yet).

## When Lichii updates

1. The Lichii Builds tray app says so (and the Session tab's row shows "NEW BUILD", read from that app). Install it from the tray app; it updates the build's own folder in place and archives the old one. Frizti stays the default launch build.
2. Launch from the tool, get in-game, then **Connect** and **Self-test**.

Nothing in the tool depends on Lichii's internals. These are the only things it relies on:
- the debug server's JSON protocol (`status`, `read_memory`, `search_memory`)
- the boot-log line `Module main loaded at 0x... (size 0x...)`
- fastmem being on (the default)

If a future build breaks one of these, the self-test names the failing channel.

## When the game updates

- **Rebuild flag names** (`names_build.py`) rebuilds `names.tsv` from the new romfs. `python names_build.py --lists-only` only merges the name lists and code strings, and takes about a second.
- **Rebuild game catalog** regenerates the menus' lists. **Ghidra ELF of main** handles the new `main`.
- Cheats, watches, the catalog and the dumps are keyed by the game's build ID, so old addresses won't silently apply to a new build.
- Live GameData needs two numbers per game build, in `gdlive.py` (`BUILDS`): the offset of the `gmd::GameDataMgr` pointer in `main`'s .bss, and the record vtables. On an unknown build, Connect says so and everything else keeps working. To map a new build, run the v0.4 probes. They find the tables by their contents, and the pointer to them, even when the layout moves.

## Troubleshooting

- **The Warp, Items or Loadout lists are empty / searching finds nothing.** The game catalog hasn't been built yet. It is generated from your own dump and is not part of the download. Since v0.8.7 it builds itself on the first start (about a minute); otherwise click **Rebuild game catalog** on the Session tab. If it still fails, the Session tab must show your dump folder (Detected -> dump); if it says none, use **Change folders**.
- **Flags show as `#XXXXXXXX` instead of names.** `names.tsv` isn't built yet: Session tab -> **Rebuild flag names** (a few minutes).
- **"Missing Python package zstandard".** Run the pip line under "Install and first run", or click **Install missing packages**, then restart the tool.
- **Double-clicking devtool.pyw does nothing.** Run `python devtool.pyw` from a command prompt in the tool's folder to see the error; usually a missing package or a Python without tkinter.
- The line at the top of the window shows the connection state: **Not connected**, **Connected (reads only)**, or **Connected: reads + writes**. Results and errors appear next to it. The tool doesn't use popups, because they can open behind a fullscreen game.
- Everything the tool does is also written to `user\devtool.log`. That includes the lines from Lichii's own log about mods, patches and the debug server, which show whether a RomFS mod such as DevTool Camera loaded ("Applied RomFS patches (LayeredFS)"). If something doesn't work, that file is what to send.

## Notes

- Edit saves while you're at the title screen, then load the slot. The game overwrites a slot whenever it saves, and only reads it when you load it.
- `progress.sav` is the Tears of the Kingdom GameData format: revision `2982123` and murmur3-hashed names. It has no checksum as far as we know, so one in-game load test confirms edits are accepted.
- Writes from outside the game only work on data. Code edits made this way are invisible to Lichii's JIT, so code changes have to be `.pchtxt` patches.
- Not in v0.1: warping to any scene and proper give-item (both need the game's own functions found in Ghidra), noclip, and free camera. That's the next phase.
