"""Synthetic structural tests; private player saves are never test fixtures."""
import struct
import unittest

from tools.inspect_save import FormatError, candidate_counter_tables, inspect_bytes


def u32(value):
    return struct.pack("<I", value)


def latin(value):
    encoded = value.encode("latin-1") + b"\0"
    return u32(len(encoded)) + encoded


def synthetic_save():
    data = latin("fixture.ees") + latin("Test") + u32(0) + u32(231)
    data += latin("Player With Spaces Other") + latin("Saved timestamp") + b"\0"
    for slot in range(16):
        name = "Player With Spaces" if slot == 0 else "Other" if slot == 1 else ""
        data += b"\0\0\0" + u32(1 if name else 4) + latin(name)
        data += struct.pack("<fIIII", 0, slot, slot, slot + 1 if name else 0, 20 if name else 0)
    data += b"\0\0\0" + u32(0) * 10 + b"\0" * 8
    data += latin("") + latin("") + u32(0) + b"\0" + latin("") + u32(0)
    data += u32(1) + u32(2) + latin("") * 7 + u32(42)
    data += u32(0) + u32(231) + u32(2) + u32(4) + u32(1337)
    return u32(len(data)) + data


def candidate_table(name="Player With Spaces", count=1):
    data = latin(name) + u32(0) + u32(31)
    for index in range(31):
        data += u32(index * 10) + b"\0" + u32(count)
        data += (u32(index * 10) + u32(100 + index)) * count
    return data


class SaveInspectorTests(unittest.TestCase):
    def test_structural_metadata_round_trip_preserves_name_spaces_and_raw_flags(self):
        raw = synthetic_save()
        metadata, extracted = inspect_bytes(raw, decompress=True)
        self.assertEqual(metadata["saved_name"], "fixture.ees")
        self.assertEqual(metadata["version"], [0, 231])
        self.assertEqual(len(metadata["players"]), 16)
        self.assertEqual(metadata["players"][0]["name"], "Player With Spaces")
        self.assertEqual(metadata["players"][1]["team"], 2)
        self.assertEqual(metadata["players"][2]["flags_raw"], 4)
        self.assertEqual(metadata["system_uptime_ms"], 42)
        self.assertEqual(extracted, [("00-2-Seed.bin", u32(1337))])
        self.assertFalse(metadata["military_stats_verified"])
        self.assertFalse(metadata["winner_verified"])

    def test_truncated_inputs_rejected(self):
        raw = synthetic_save()
        for size in (0, 3, 10, len(raw) - 1, len(raw) - 12):
            truncated = raw[:size]
            if size >= 4:
                truncated = u32(size - 4) + truncated[4:]
            with self.subTest(size=size), self.assertRaises(FormatError):
                inspect_bytes(truncated)

    def test_size_prefix_and_string_bounds_checked(self):
        raw = synthetic_save()
        variants = [u32(1) + raw[4:], raw[:4] + u32(0xFFFFFFFF) + raw[8:],
                    raw[:4] + u32(0) + raw[8:]]
        for corrupted in variants:
            with self.subTest(corrupted=corrupted[:12]), self.assertRaises(FormatError):
                inspect_bytes(corrupted)

    def test_lump_bounds_type_and_trailing_data_checked(self):
        raw = synthetic_save()
        variants = [raw[:-8] + u32(0xFFFFFFFF) + raw[-4:],
                    raw[:-12] + u32(5) + raw[-8:],
                    u32(len(raw)) + raw[4:] + b"junk"]
        for corrupted in variants:
            with self.subTest(tail=corrupted[-20:]), self.assertRaises(FormatError):
                inspect_bytes(corrupted)

    def test_unverified_counter_candidates_preserve_numeric_ids_and_timestamps(self):
        table = candidate_table()
        records = candidate_counter_tables(b"prefix" + table, ["Player With Spaces", "Absent"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["decoded_offset"], 6)
        self.assertFalse(records[0]["counter_meanings_verified"])
        self.assertEqual(records[0]["counters"][20]["current_raw"], 200)
        self.assertEqual(records[0]["counters"][20]["last_pair_raw"], [200, 120])

    def test_counter_candidates_reject_truncation_ambiguity_and_excessive_history(self):
        table = candidate_table()
        oversized_count = latin("Player With Spaces") + u32(0) + u32(31) + u32(0) + b"\0" + u32(0xFFFFFFFF)
        for data in (table[:-1], table + table, oversized_count):
            with self.subTest(size=len(data)):
                self.assertEqual(candidate_counter_tables(data, ["Player With Spaces"]), [])

    def test_literal_question_marks_are_preserved(self):
        records = candidate_counter_tables(candidate_table("???????"), ["???????"])
        self.assertEqual(records[0]["saved_player_name"], "???????")


if __name__ == "__main__":
    unittest.main()
