"""Read-only research inspector for original Empire Earth .ees / .scn files.

This parses structural metadata, not verified military counters or a winner.
No input file is modified. Use --extract-dir only for a separate research folder.

Format reference: https://github.com/coderbot16/scn/wiki/EE1-and-EEAOC-Format
PK01 reference: https://github.com/EE-modders/Empire-Earth-toolbox/wiki/SSA-v1.0
Optional decompression: python -m pip install dclimplode==0.0.1.0
Windows wheels are available for Python 3.11; newer Python may need a compiler.

The documentation's player flag meanings are explicitly unverified. Report them
as raw flags: never infer victory, defeat, active status, or MMR eligibility.
The inspected Neo Empire Earth original saves use 16 fixed header player slots.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct


MAX_FILE_BYTES = 128 * 1024 * 1024
LUMP_TYPES = {1: "Unknown1", 2: "Seed", 3: "CameraPosition", 4: "Terrain",
              5: "PlayerData", 6: "Triggers", 7: "Misc", 8: "Unknown8",
              10: "GfxEffects", 11: "Calamities", 12: "Unknown12"}


class FormatError(ValueError):
    pass


class Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, count):
        if count < 0 or count > len(self.data) - self.offset:
            raise FormatError(f"Truncated or invalid field at offset {self.offset} (length {count}).")
        result = self.data[self.offset:self.offset + count]
        self.offset += count
        return result

    def u8(self):
        return self.take(1)[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def f32(self):
        return struct.unpack("<f", self.take(4))[0]

    def string(self):
        size = self.u32()
        if not 1 <= size <= 1024 * 1024:
            raise FormatError(f"Invalid Latin string size {size} at offset {self.offset - 4}.")
        value = self.take(size)
        if value[-1:] != b"\0":
            raise FormatError(f"Latin string is missing its terminator at offset {self.offset - size}.")
        return value[:-1].decode("latin-1")


def decode_pk01(data):
    """Optionally decompress a PK01 lump and validate its stated output length."""
    if not data.startswith(b"PK01"):
        return data
    if len(data) < 14:
        raise FormatError("Truncated PK01 header.")
    expected_size, reserved = struct.unpack_from("<II", data, 4)
    if expected_size > MAX_FILE_BYTES or reserved != 0:
        raise FormatError("Invalid or unsupported PK01 compression header.")
    try:
        import dclimplode
    except ImportError as exc:
        raise FormatError("Install optional dclimplode==0.0.1.0 to decompress lumps.") from exc
    try:
        decompressor = dclimplode.decompressobj()
        result = decompressor.decompress(data[12:])
    except Exception as exc:
        raise FormatError(f"DCL decompression failed: {exc}") from exc
    if not decompressor.eof or len(result) != expected_size:
        raise FormatError(f"PK01 expected {expected_size} bytes, decoded {len(result)}.")
    return result


def inspect_bytes(data, decompress=False):
    if len(data) > MAX_FILE_BYTES:
        raise FormatError("Save exceeds the inspector's 128 MiB limit.")
    reader = Reader(data)
    declared_size = reader.u32()
    if declared_size != len(data) - 4:
        raise FormatError(f"Size prefix {declared_size} does not match file size {len(data)}.")
    result = {"file_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
              "saved_name": reader.string(), "description": reader.string(),
              "version": [reader.u32(), reader.u32()],
              "player_list_text": reader.string(), "saved_date_text": reader.string()}
    if result["version"] != [0, 231]:
        raise FormatError(f"Only observed original-game save version 0.231 is supported, got {result['version']}.")
    result["header_unknown0"] = reader.u8()
    players = []
    for slot in range(16):
        players.append({"slot": slot, "unknown0": reader.u8(), "unknown1": reader.u8(),
                        "shared_los_raw": reader.u8(), "flags_raw": reader.u32(),
                        "name": reader.string(), "unknown_float": reader.f32(),
                        "player_id": reader.u32(), "color_index": reader.u32(),
                        "team": reader.u32(), "starting_citizens": reader.u32()})
    result["players"] = players
    settings = {"unknown_bytes": list(reader.take(3))}
    for field in ("game_speed", "unknown3", "game_variant", "map_size", "start_epoch",
                  "end_epoch", "resources", "max_units", "wonders_for_victory", "ai_difficulty"):
        settings[field] = reader.u32()
    for field in ("victory_allowed", "lock_teams", "lock_speed", "reveal_map",
                  "cheats", "unknown4", "custom_civs", "unknown6"):
        settings[field] = reader.u8()
    result["initial_settings"] = settings
    result["campaign_name"] = reader.string()
    result["scenario_name"] = reader.string()
    result["header_unknown1"] = reader.u32()
    result["header_unknown2"] = reader.u8()
    result["forced_name"] = reader.string()
    result["header_unknown3"] = reader.u32()
    lump_count = reader.u32()
    if not 1 <= lump_count <= 64:
        raise FormatError(f"Unsupported lump count {lump_count}.")
    expected_types = [reader.u32() for _ in range(lump_count)]
    result["scenario_text"] = {name: reader.string() for name in (
        "unknown", "hints", "history", "movie", "map", "instructions", "soundover")}
    result["system_uptime_ms"] = reader.u32()
    result["header_bytes"] = reader.offset
    lumps = []
    extracted = []
    for index, expected_type in enumerate(expected_types):
        offset = reader.offset
        version = [reader.u32(), reader.u32()]
        lump_type = reader.u32()
        size = reader.u32()
        if lump_type != expected_type or version != result["version"]:
            raise FormatError(f"Lump {index} type/version differs from the header.")
        payload = reader.take(size)
        compressed = payload.startswith(b"PK01")
        uncompressed_size = struct.unpack_from("<I", payload, 4)[0] if compressed and size >= 12 else size
        info = {"index": index, "type": lump_type, "name": LUMP_TYPES.get(lump_type, "Unknown"),
                "offset": offset, "stored_bytes": size, "compressed": compressed,
                "uncompressed_bytes": uncompressed_size}
        if decompress:
            decoded = decode_pk01(payload)
            info["decoded_sha256"] = hashlib.sha256(decoded).hexdigest()
            extracted.append((f"{index:02d}-{lump_type}-{info['name']}.bin", decoded))
        lumps.append(info)
    if reader.offset != len(data):
        raise FormatError(f"Unexpected trailing bytes after lumps: {len(data) - reader.offset}.")
    result["lumps"] = lumps
    result["military_stats_verified"] = False
    result["winner_verified"] = False
    return result, extracted


def candidate_counter_tables(player_data, names):
    """Locate experimental name-anchored tables, leaving all counter IDs unnamed.

    Observed in original 0.231 saves: name + u32 zero + u32 31, followed by
    31 records (u32 current, u8 history flag, u32 count, count * (u32,u32)).
    Pair values resemble (value, simulation timestamp), but this is a research
    hypothesis. A matching counter is not proof of its meaning or finality.
    Return only unambiguous, bounded records. Never use this output to rate games.
    """
    tables = []
    for name in names:
        if not name:
            continue
        encoded = name.encode("latin-1") + b"\0"
        pattern = struct.pack("<I", len(encoded)) + encoded + struct.pack("<II", 0, 31)
        start = 0
        candidates = []
        while (start := player_data.find(pattern, start)) >= 0:
            anchor = start
            reader = Reader(player_data)
            reader.offset = start + len(pattern)
            records = []
            try:
                for index in range(31):
                    current, flag, count = reader.u32(), reader.u8(), reader.u32()
                    if flag not in (0, 1) or not 1 <= count <= 100_000:
                        raise FormatError("Invalid candidate counter structure.")
                    history = reader.take(count * 8)
                    records.append({"counter_id": index, "current_raw": current,
                                    "history_flag_raw": flag, "history_count": count,
                                    "first_pair_raw": list(struct.unpack_from("<II", history)),
                                    "last_pair_raw": list(struct.unpack_from("<II", history, len(history) - 8))})
                candidates.append({"saved_player_name": name, "decoded_offset": anchor,
                                   "counter_meanings_verified": False, "counters": records})
            except FormatError:
                pass
            start += 1
        if len(candidates) == 1:
            tables.append(candidates[0])
    return tables


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="A local .ees or .scn save (read only).")
    parser.add_argument("--decompress", action="store_true", help="Validate and hash decompressed lump contents.")
    parser.add_argument("--raw-counters", action="store_true", help="Show experimental unnamed counter tables; never final results.")
    parser.add_argument("--extract-dir", type=Path, help="Write decoded .bin lumps in a separate new folder.")
    args = parser.parse_args()
    source = args.path.resolve()
    try:
        if source.stat().st_size > MAX_FILE_BYTES:
            parser.error("Save exceeds the inspector's 128 MiB limit.")
        result, extracted = inspect_bytes(source.read_bytes(), args.decompress or args.raw_counters or args.extract_dir is not None)
        if args.raw_counters:
            player_data = next((data for name, data in extracted if name.endswith("-5-PlayerData.bin")), None)
            result["experimental_counter_tables"] = candidate_counter_tables(
                player_data, [p["name"] for p in result["players"]]) if player_data else []
        if args.extract_dir:
            destination = args.extract_dir.resolve()
            if destination == source.parent or source.parent in destination.parents:
                parser.error("Choose an extraction folder outside the game's save directory.")
            destination.mkdir(parents=True, exist_ok=False)
            for name, payload in extracted:
                (destination / name).write_bytes(payload)
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    except (FormatError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
