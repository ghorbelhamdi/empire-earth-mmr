# Original Empire Earth save inspection

`tools/inspect_save.py` reads local `.ees`/`.scn` files without modifying them.
It supports the observed original-game save version **0.231**. It checks the
overall size prefix, string bounds, the fixed 16-player header, lump types and
versions, and complete consumption of the file. It reports names, team and color
IDs, initial match settings, and lump sizes. Empty player slots remain visible.
It never infers a winner or produces military statistics for rating submission.

The original reference is [coderbot16's EE1/EEAOC format documentation](https://github.com/coderbot16/scn/wiki/EE1-and-EEAOC-Format).
The documented player flag meanings are explicitly unverified, so the inspector
preserves their raw values. Initial settings also need not describe the final
state of a game. A name containing spaces is read from its own length-prefixed
field; splitting the header's combined player list would corrupt such names.

## Decompression

Compressed lumps begin with `PK01`, a little-endian uncompressed size, a reserved
zero word, and a PKWARE DCL Implode stream. This is consistent with the
[community PK01 description](https://github.com/EE-modders/Empire-Earth-toolbox/wiki/SSA-v1.0).
The optional [dclimplode library](https://pypi.org/project/dclimplode/) decodes
these streams; the inspector checks both end-of-stream and the declared size.

```powershell
python tools/inspect_save.py "C:\research\sample.ees"
python -m pip install dclimplode==0.0.1.0
python tools/inspect_save.py "C:\research\sample.ees" --decompress
python tools/inspect_save.py "C:\research\sample.ees" --extract-dir "C:\research\decoded-new"
python tools/inspect_save.py "C:\research\sample.ees" --raw-counters
```

For Windows, dclimplode 0.0.1.0 provides a Python 3.11 wheel. Installing it under
Python 3.12 can require Microsoft C++ build tools. The metadata-only inspector
has no third-party dependency. Development research used an isolated Python
3.11 runtime to validate decompression; no game files or installed game DLLs
were changed. Extracted files can include private in-game chat: keep them local.

## Experimental counter tables

The `--raw-counters` flag searches the decompressed PlayerData lump for a bounded,
unambiguous candidate table anchored to each exact saved player name. The
observed candidate has a zero word, a count of 31 records, and records containing
an integer current value, a byte flag, and a counted list of integer pairs. The
pairs resemble value/timestamp history. The tool exposes numeric counter IDs
and raw pairs without assigning military meanings.

In a supplied screenshot/save comparison, candidate IDs 20 and 21 agreed with
the hotkey and mouse-click totals of a player who had already left the game.
Most other players' snapshot values were below the later screenshot totals.
This supports further investigation, but it does **not** validate every counter
or make a mid-game autosave equivalent to final post-game statistics. The tool
marks all candidate meanings, military statistics, and victory as unverified.

Some local dreXmod logs also contain `GAME_END` events with player indices and
`VictoryFlag` values. Their logs mention score-submission requests without the
underlying counter payload. Mapping an event to a named player, the correct
match, and a complete team result still needs validation. A player can leave
before teammates win, so a single player's event cannot establish a team result.

## Validating military counters

1. Use a small controlled match with distinct player names and a known game build.
2. Save at a recorded point and capture the Military statistics for that same
   point, with all players and columns visible. If loading a copy is needed,
   avoid advancing the simulation before capturing the comparison.
3. Repeat after one known unit is killed and compare the decompressed candidate
   counters. Check both killer and victim; repeat with different unit classes
   and building losses so military units, civilians, and buildings are separated.
4. Repeat across more than one game and with zero kills/losses. Validate all
   player IDs, team assignments, game time, counter locations, and version gates.
5. Confirm that a final save really contains final counters and a complete result
   before proposing an importer. Continue showing the user a review screen and
   keep the server's pending-match approval flow.

The current companion uses reviewed screenshot/manual statistics. Save inspection
is a separate research tool; its output is not connected to the rating API.
