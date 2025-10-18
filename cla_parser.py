#!/usr/bin/env python3
"""
CLA Raid Debuff Coverage generator for WotLK 3.3.5 combat logs.

Reads a raw combat log, detects boss encounters, tracks key raid debuffs,
and emits a JSON table that mirrors the structure of the original CLA spreadsheet.
On first run the script also writes `cla_map.json` (spell/category metadata).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple

# Add log converter directory to import boss helpers
REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_CONVERTER_DIR = REPO_ROOT / "log_converter_3.3.5-3.4"
if LOG_CONVERTER_DIR.exists():
    sys.path.append(str(LOG_CONVERTER_DIR))

try:
    from encounters import detect_encounters  # type: ignore
    from guid import boss_name_for_guid, get_guid_type, is_boss_guid  # type: ignore
    from timestamp import LogClock, split_arg0  # type: ignore
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit(
        "Unable to import encounter helpers. Ensure encounters.py, guid.py, and timestamp.py are available (either beside cla_parser.py or in the legacy log_converter_3.3.5-3.4 directory)."
    ) from exc

try:
    from render_cla_heatmap import render_heatmap  # type: ignore
except ImportError:  # pragma: no cover - runtime guard
    render_heatmap = None  # type: ignore


MAP_PATH = Path(__file__).with_name("cla_map.json")

APPLY_EVENTS = {"SPELL_AURA_APPLIED", "SPELL_AURA_APPLIED_DOSE"}
REFRESH_EVENTS = {"SPELL_AURA_REFRESH"}
REMOVE_EVENTS = {"SPELL_AURA_REMOVED", "SPELL_AURA_BROKEN", "SPELL_AURA_BROKEN_SPELL"}

# Category indices that feed into overall calculation
OFF_NO_CRIT_INDICES = [0, 1, 3, 4, 5]
REDUCTION_INDICES = [6, 7, 8, 9, 10]


# ---------------------------- Data classes --------------------------------- #

@dataclass
class SpellConfig:
    spell_id: int
    name: str
    clazz: str
    type: str  # "boss_debuff" or "raid_buff"
    aliases: List[str]


@dataclass
class CategoryConfig:
    key: str
    label: str
    group: str
    classes: List[str]
    effect: Optional[str]
    spells: List[SpellConfig]


@dataclass
class Event:
    index: int
    timestamp: float
    event_type: str
    source_guid: str
    dest_guid: str
    spell_id: Optional[int]
    spell_name: Optional[str]


@dataclass
class FightWindow:
    start_idx: int
    end_idx: int
    name: str
    raid_size: int
    start_time: float
    end_time: float


class CategoryTracker:
    """Tracks union uptime and per-spell contributions for a single category."""

    def __init__(self, category_key: str):
        self.category_key = category_key
        self._active_instances: Dict[Tuple[int, str], float] = {}
        self._union_active_count = 0
        self._union_current_start: Optional[float] = None
        self._union_intervals: List[Tuple[float, float]] = []
        self._spell_totals: DefaultDict[int, float] = defaultdict(float)

    # ----------------- state helpers ----------------- #
    def activate(self, key: Tuple[int, str], timestamp: float):
        if key in self._active_instances:
            return
        self._active_instances[key] = timestamp
        if self._union_active_count == 0:
            self._union_current_start = timestamp
        self._union_active_count += 1

    def deactivate(self, key: Tuple[int, str], timestamp: float):
        start = self._active_instances.pop(key, None)
        if start is not None:
            self._spell_totals[key[0]] += max(0.0, timestamp - start)
        if self._union_active_count > 0 and key not in self._active_instances:
            self._union_active_count -= 1
            if self._union_active_count == 0 and self._union_current_start is not None:
                end_time = max(timestamp, self._union_current_start)
                self._union_intervals.append((self._union_current_start, end_time))
                self._union_current_start = None

    def refresh(self, key: Tuple[int, str], timestamp: float):
        if key not in self._active_instances:
            self.activate(key, timestamp)

    def force_active(self, key: Tuple[int, str], timestamp: float):
        """Activate at a specific timestamp without double-counting."""
        self.activate(key, timestamp)

    def finalize(self, end_time: float):
        for key, start in list(self._active_instances.items()):
            self._spell_totals[key[0]] += max(0.0, end_time - start)
        self._active_instances.clear()
        if self._union_active_count > 0 and self._union_current_start is not None:
            end_time = max(end_time, self._union_current_start)
            self._union_intervals.append((self._union_current_start, end_time))
        self._union_active_count = 0
        self._union_current_start = None

    # ----------------- reporting helpers ----------------- #
    def uptime(self, window_start: float, window_end: float) -> float:
        duration = max(0.0, window_end - window_start)
        if duration <= 0:
            return 0.0
        total = 0.0
        for start, end in self._union_intervals:
            clamped_start = max(start, window_start)
            clamped_end = min(end, window_end)
            if clamped_end > clamped_start:
                total += clamped_end - clamped_start
        return min(1.0, total / duration)

    def spell_uptimes(
        self, window_start: float, window_end: float
    ) -> Dict[int, float]:
        duration = max(0.0, window_end - window_start)
        if duration <= 0:
            return {}
        return {
            spell_id: min(1.0, seconds / duration)
            for spell_id, seconds in self._spell_totals.items()
            if seconds > 0
        }


# --------------------------- Mapping helpers ------------------------------- #

def normalize_spell_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def default_mapping() -> Dict[str, object]:
    categories = [
        {
            "key": "bleed",
            "label": "Bleed Damage",
            "group": "Offensive Debuffs",
            "effect": "30% bleed damage",
            "classes": ["Druid", "Hunter", "Warrior"],
            "spells": [
                {"id": 48564, "name": "Mangle (Bear)", "class": "Druid", "type": "boss_debuff"},
                {"id": 48566, "name": "Mangle (Cat)", "class": "Druid", "type": "boss_debuff"},
                {"id": 57386, "name": "Stampede", "class": "Hunter", "type": "boss_debuff"},
                {"id": 46857, "name": "Trauma", "class": "Warrior", "type": "boss_debuff"},
            ],
        },
        {
            "key": "physical_damage",
            "label": "Physical Damage",
            "group": "Offensive Debuffs",
            "effect": "4% physical damage",
            "classes": ["Rogue", "Warrior"],
            "spells": [
                {"id": 58683, "name": "Savage Combat", "class": "Rogue", "type": "boss_debuff"},
                {"id": 413763, "name": "Blood Frenzy", "class": "Warrior", "type": "boss_debuff"},
            ],
        },
        {
            "key": "crit",
            "label": "Crit",
            "group": "Offensive Debuffs",
            "effect": "3% melee/ranged crit",
            "classes": ["Paladin", "Rogue", "Shaman"],
            "spells": [
                {"id": 54499, "name": "Heart of the Crusader", "class": "Paladin", "type": "boss_debuff"},
                {"id": 57970, "name": "Master Poisoner", "class": "Rogue", "type": "boss_debuff"},
                {"id": 63283, "name": "Totem of Wrath", "class": "Shaman", "type": "raid_buff"},
            ],
        },
        {
            "key": "spell_crit",
            "label": "Spell Crit",
            "group": "Offensive Debuffs",
            "effect": "5% spell crit",
            "classes": ["Mage", "Warlock"],
            "spells": [
                {"id": 12579, "name": "Winter's Chill", "class": "Mage", "type": "boss_debuff"},
                {"id": 63094, "name": "Winter's Chill (Frostfire)", "class": "Mage", "type": "boss_debuff"},
                {"id": 22959, "name": "Improved Scorch", "class": "Mage", "type": "boss_debuff"},
                {"id": 12873, "name": "Improved Scorch (Rank 3)", "class": "Mage", "type": "boss_debuff"},
                {"id": 17800, "name": "Shadow Mastery", "class": "Warlock", "type": "boss_debuff"},
            ],
        },
        {
            "key": "spell_hit",
            "label": "Spell Hit",
            "group": "Offensive Debuffs",
            "effect": "3% spell hit",
            "classes": ["Druid", "Priest"],
            "spells": [
                {"id": 770, "name": "Faerie Fire", "class": "Druid", "type": "boss_debuff"},
                {"id": 33198, "name": "Misery", "class": "Priest", "type": "boss_debuff"},
            ],
        },
        {
            "key": "spell_damage",
            "label": "Spell Damage",
            "group": "Offensive Debuffs",
            "effect": "13% spell damage",
            "classes": ["Deathknight", "Druid", "Warlock"],
            "spells": [
                {"id": 51735, "name": "Ebon Plague", "class": "Deathknight", "type": "boss_debuff"},
                {"id": 60433, "name": "Earth and Moon", "class": "Druid", "type": "boss_debuff"},
                {"id": 47865, "name": "Curse of the Elements", "class": "Warlock", "type": "boss_debuff"},
            ],
        },
        {
            "key": "armor_major",
            "label": "Armor (Major)",
            "group": "Reduction Debuffs",
            "effect": "20% armor reduction",
            "classes": ["Hunter", "Rogue", "Warrior"],
            "spells": [
                {"id": 58567, "name": "Sunder Armor", "class": "Warrior", "type": "boss_debuff"},
                {"id": 8647, "name": "Expose Armor", "class": "Rogue", "type": "boss_debuff"},
                {"id": 48669, "name": "Expose Armor (Rank 5)", "class": "Rogue", "type": "boss_debuff"},
                {"id": 55754, "name": "Acid Spit", "class": "Hunter", "type": "boss_debuff"},
                {"id": 59270, "name": "Corrosive Spit", "class": "Hunter", "type": "boss_debuff"},
                {"id": 66880, "name": "Acid Spit (Worm)", "class": "Hunter", "type": "boss_debuff"},
            ],
        },
        {
            "key": "armor_minor",
            "label": "Armor (Minor)",
            "group": "Reduction Debuffs",
            "effect": "5% armor reduction",
            "classes": ["Druid", "Hunter", "Warlock"],
            "spells": [
                {"id": 50511, "name": "Curse of Weakness", "class": "Warlock", "type": "boss_debuff"},
                {"id": 770, "name": "Faerie Fire", "class": "Druid", "type": "boss_debuff"},
                {"id": 16857, "name": "Faerie Fire (Feral)", "class": "Druid", "type": "boss_debuff"},
                {"id": 56631, "name": "Sting", "class": "Hunter", "type": "boss_debuff"},
            ],
        },
        {
            "key": "attack_speed",
            "label": "Attack Speed",
            "group": "Reduction Debuffs",
            "effect": "20% attack speed slow",
            "classes": ["Deathknight", "Druid", "Paladin", "Warrior"],
            "spells": [
                {"id": 47502, "name": "Thunder Clap", "class": "Warrior", "type": "boss_debuff"},
                {"id": 68055, "name": "Judgements of the Just", "class": "Paladin", "type": "boss_debuff"},
                {"id": 48485, "name": "Infected Wounds", "class": "Druid", "type": "boss_debuff"},
                {"id": 58181, "name": "Infected Wounds (Rank 2)", "class": "Druid", "type": "boss_debuff"},
                {"id": 55095, "name": "Frost Fever", "class": "Deathknight", "type": "boss_debuff"},
            ],
        },
        {
            "key": "attack_power",
            "label": "Attack Power",
            "group": "Reduction Debuffs",
            "effect": "574 attack power reduction",
            "classes": ["Druid", "Paladin", "Warlock", "Warrior"],
            "spells": [
                {"id": 47437, "name": "Demoralizing Shout", "class": "Warrior", "type": "boss_debuff"},
                {"id": 48560, "name": "Demoralizing Roar", "class": "Druid", "type": "boss_debuff"},
                {"id": 50511, "name": "Curse of Weakness", "class": "Warlock", "type": "boss_debuff"},
                {"id": 26017, "name": "Vindication", "class": "Paladin", "type": "boss_debuff"},
            ],
        },
        {
            "key": "physical_hit",
            "label": "Physical Hit",
            "group": "Reduction Debuffs",
            "effect": "3% physical hit",
            "classes": ["Druid", "Hunter"],
            "spells": [
                {"id": 48468, "name": "Insect Swarm", "class": "Druid", "type": "boss_debuff"},
                {"id": 3043, "name": "Scorpid Sting", "class": "Hunter", "type": "boss_debuff"},
            ],
        },
        {
            "key": "crystal_yield",
            "label": "Armor (C. Yield)",
            "group": "Reduction Debuffs",
            "effect": "20% armor reduction",
            "classes": ["Anyone"],
            "spells": [
                {"id": 15235, "name": "Crystal Yield", "class": "Any", "type": "boss_debuff"},
            ],
        },
    ]
    for category in categories:
        for spell in category["spells"]:
            spell.setdefault("aliases", [])
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": categories,
    }


def load_mapping() -> List[CategoryConfig]:
    if not MAP_PATH.exists():
        MAP_PATH.write_text(json.dumps(default_mapping(), indent=2), encoding="utf-8")
    with MAP_PATH.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    categories: List[CategoryConfig] = []
    for cat in data["categories"]:
        spells = [
            SpellConfig(
                spell_id=int(sp["id"]),
                name=sp["name"],
                clazz=sp.get("class", "Unknown"),
                type=sp.get("type", "boss_debuff"),
                aliases=[normalize_spell_name(alias) for alias in sp.get("aliases", [])],
            )
            for sp in cat["spells"]
        ]
        categories.append(
            CategoryConfig(
                key=cat["key"],
                label=cat["label"],
                group=cat["group"],
                classes=cat["classes"],
                effect=cat.get("effect"),
                spells=spells,
            )
        )
    return categories


# ------------------------- Parsing utilities -------------------------------- #

def parse_log_rows(path: Path) -> List[List[str]]:
    rows: List[List[str]] = []
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.reader(handle, delimiter=",", quotechar='"', escapechar="\\")
        for cols in reader:
            if cols:
                rows.append(cols)
    return rows


def build_events(rows: Sequence[Sequence[str]]) -> List[Event]:
    events: List[Event] = []
    clock = LogClock()
    for idx, row in enumerate(rows):
        arg0 = row[0]
        calendar, event_type = split_arg0(arg0)
        timestamp = clock.to_seconds(calendar)
        source_guid = row[1] if len(row) > 1 else ""
        dest_guid = row[4] if len(row) > 4 else ""
        spell_id: Optional[int] = None
        spell_name: Optional[str] = None
        if len(row) > 7:
            maybe_id = row[7]
            if maybe_id:
                try:
                    spell_id = int(maybe_id)
                except ValueError:
                    spell_id = None
        if len(row) > 8:
            maybe_name = row[8]
            if maybe_name:
                spell_name = maybe_name
        events.append(
            Event(
                index=idx,
                timestamp=timestamp,
                event_type=event_type,
                source_guid=source_guid,
                dest_guid=dest_guid,
                spell_id=spell_id,
                spell_name=spell_name,
            )
        )
    return events


def detect_fights(rows: List[List[str]], events: List[Event]) -> List[FightWindow]:
    fights: List[FightWindow] = []
    encounter_ranges = detect_encounters(rows)
    for start_idx, end_idx, encounter_name, _, _, raid_size in encounter_ranges:
        if start_idx < 0 or end_idx >= len(events):
            continue
        start_time = events[start_idx].timestamp
        end_time = events[end_idx].timestamp
        if end_time < start_time:
            end_time = start_time
        fights.append(
            FightWindow(
                start_idx=start_idx,
                end_idx=end_idx,
                name=encounter_name,
                raid_size=raid_size,
                start_time=start_time,
                end_time=end_time,
            )
        )
    return fights


# ----------------------- Coverage computation -------------------------------- #

def build_spell_index(categories: List[CategoryConfig]):
    by_id: Dict[int, List[Tuple[int, SpellConfig]]] = defaultdict(list)
    by_alias: Dict[str, List[Tuple[int, SpellConfig]]] = defaultdict(list)
    for idx, category in enumerate(categories):
        for spell in category.spells:
            by_id[spell.spell_id].append((idx, spell))
            alias_keys = {normalize_spell_name(spell.name)}
            alias_keys.update(spell.aliases)
            for alias in alias_keys:
                if alias:
                    by_alias[alias].append((idx, spell))
    return by_id, by_alias


def format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    secs = int(round(seconds - minutes * 60))
    if secs == 60:
        minutes += 1
        secs = 0
    return f"{minutes}:{secs:02d}"


def compute_overall(uptimes: List[float]) -> float:
    off_vals = [uptimes[i] for i in OFF_NO_CRIT_INDICES]
    reduc_vals = [uptimes[i] for i in REDUCTION_INDICES]
    if not off_vals or not reduc_vals:
        return 0.0
    off_avg = sum(off_vals) / len(off_vals)
    reduc_avg = sum(reduc_vals) / len(reduc_vals)
    return (off_avg + reduc_avg) / 2


def event_categories(
    event: Event,
    spell_index: Dict[int, List[Tuple[int, SpellConfig]]],
    alias_index: Dict[str, List[Tuple[int, SpellConfig]]],
) -> List[Tuple[int, SpellConfig]]:
    matches: List[Tuple[int, SpellConfig]] = []
    if event.spell_id and event.spell_id in spell_index:
        matches.extend(spell_index[event.spell_id])
    elif event.spell_name:
        alias = normalize_spell_name(event.spell_name)
        if alias and alias in alias_index:
            matches.extend(alias_index[alias])
    return matches


def destination_is_valid(spell: SpellConfig, dest_guid: str) -> bool:
    if spell.type == "raid_buff":
        dest_type = get_guid_type(dest_guid)
        return dest_type in {"player", "pet", "vehicle"}
    return is_boss_guid(dest_guid)


def track_fight(
    fight: FightWindow,
    events: List[Event],
    categories: List[CategoryConfig],
    global_active: Dict[Tuple[int, str], SpellConfig],
    spell_index: Dict[int, List[Tuple[int, SpellConfig]]],
    alias_index: Dict[str, List[Tuple[int, SpellConfig]]],
) -> Dict[str, object]:
    trackers = [CategoryTracker(cat.key) for cat in categories]
    start = fight.start_time
    end = fight.end_time

    # Reactivate any relevant aura already up at pull
    for key, spell in list(global_active.items()):
        for cat_idx, cat_spell in spell_index.get(key[0], []):
            if cat_spell is spell and destination_is_valid(cat_spell, key[1]):
                trackers[cat_idx].force_active(key, start)

    boss_died = False
    for idx in range(fight.start_idx, fight.end_idx + 1):
        event = events[idx]
        if event.event_type == "UNIT_DIED" and is_boss_guid(event.dest_guid):
            boss_died = True

        matches = event_categories(event, spell_index, alias_index)
        if not matches:
            continue

        key = (event.spell_id or -1, event.dest_guid or "")
        handled = False
        for cat_idx, spell in matches:
            if not destination_is_valid(spell, event.dest_guid or ""):
                continue
            tracker = trackers[cat_idx]
            if event.event_type in APPLY_EVENTS:
                tracker.activate(key, event.timestamp)
                handled = True
            elif event.event_type in REMOVE_EVENTS:
                tracker.deactivate(key, event.timestamp)
                handled = True
            elif event.event_type in REFRESH_EVENTS:
                tracker.refresh(key, event.timestamp)
                handled = True
        if handled:
            if event.event_type in APPLY_EVENTS:
                global_active[key] = matches[0][1]
            elif event.event_type in REMOVE_EVENTS:
                global_active.pop(key, None)

    for tracker in trackers:
        tracker.finalize(end)

    uptimes = [tracker.uptime(start, end) for tracker in trackers]
    overall = compute_overall(uptimes)

    fight_duration = max(0.0, end - start)
    detail_map: Dict[str, List[Dict[str, object]]] = {}
    for cat_cfg, tracker in zip(categories, trackers):
        spell_uptimes = tracker.spell_uptimes(start, end)
        details: List[Dict[str, object]] = []
        for spell_id, uptime in sorted(
            spell_uptimes.items(), key=lambda item: item[1], reverse=True
        ):
            spell_meta = next(
                (sp for sp in cat_cfg.spells if sp.spell_id == spell_id), None
            )
            if not spell_meta:
                continue
            details.append(
                {
                    "spell_id": spell_id,
                    "spell_name": spell_meta.name,
                    "class": spell_meta.clazz,
                    "uptime": round(uptime, 4),
                }
            )
        detail_map[cat_cfg.key] = details

    start_event = events[fight.start_idx]
    boss_guid = (
        start_event.source_guid
        if is_boss_guid(start_event.source_guid)
        else start_event.dest_guid
    )
    boss_display = boss_name_for_guid(boss_guid) or fight.name
    result = "kill" if boss_died else "wipe"
    duration_label = format_duration(fight_duration)
    boss_label = (
        f"{boss_display} (kill in {duration_label})"
        if result == "kill"
        else f"{boss_display} (wipe after {duration_label})"
    )

    summary_row = {"boss": boss_label, "overall": round(overall, 4)}
    for cat_cfg, uptime in zip(categories, uptimes):
        summary_row[cat_cfg.label] = round(uptime, 4)

    return {
        "summary": summary_row,
        "details": detail_map,
        "metadata": {
            "boss": boss_display,
            "label": boss_label,
            "result": result,
            "raid_size": fight.raid_size,
            "start_time": start,
            "end_time": end,
            "duration_seconds": fight_duration,
        },
    }


def process_log(path: Path) -> Dict[str, object]:
    categories = load_mapping()
    rows = parse_log_rows(path)
    if not rows:
        raise ValueError("No entries found in combat log.")
    events = build_events(rows)
    fights = detect_fights(rows, events)
    spell_index, alias_index = build_spell_index(categories)

    global_active: Dict[Tuple[int, str], SpellConfig] = {}

    table_rows: List[Dict[str, object]] = []
    fights_out: List[Dict[str, object]] = []

    current_idx = 0
    for fight in fights:
        while current_idx < fight.start_idx:
            event = events[current_idx]
            matches = event_categories(event, spell_index, alias_index)
            if matches:
                key = (event.spell_id or -1, event.dest_guid or "")
                if event.event_type in APPLY_EVENTS:
                    spell = matches[0][1]
                    if destination_is_valid(spell, event.dest_guid or ""):
                        global_active[key] = spell
                elif event.event_type in REMOVE_EVENTS:
                    global_active.pop(key, None)
            current_idx += 1

        summary = track_fight(
            fight,
            events,
            categories,
            global_active,
            spell_index,
            alias_index,
        )
        table_rows.append(summary["summary"])
        coverage = {
            cat.label: summary["summary"][cat.label] for cat in categories
        }
        fights_out.append(
            {
                **summary["metadata"],
                "overall": summary["summary"]["overall"],
                "coverage": coverage,
                "details": summary["details"],
            }
        )
        current_idx = fight.end_idx + 1

    return {
        "log": str(path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": [
            {
                "key": cat.key,
                "label": cat.label,
                "group": cat.group,
                "classes": cat.classes,
                "effect": cat.effect,
            }
            for cat in categories
        ],
        "table": table_rows,
        "fights": fights_out,
    }


# --------------------------- CLI interface ---------------------------------- #

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate CLA raid debuff coverage JSON from a WotLK 3.3.5 combat log."
    )
    parser.add_argument("logfile", type=Path, help="Path to WoWCombatLog.txt (or similar)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Destination path for JSON output (default: stdout).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON with indentation.",
    )
    parser.add_argument(
        "--heatmap",
        action="store_true",
        help="Also render an HTML heatmap (writes alongside JSON by default).",
    )
    parser.add_argument(
        "--heatmap-output",
        type=Path,
        help="Explicit path for the HTML heatmap (implies --heatmap).",
    )
    parser.add_argument(
        "--heatmap-title",
        type=str,
        help="Custom title for the HTML heatmap (defaults to log path).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = process_log(args.logfile)
    except FileNotFoundError:
        print(f"Combat log not found: {args.logfile}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Failed to process log: {exc}", file=sys.stderr)
        return 1

    json_kwargs = {"ensure_ascii": False}
    if args.pretty:
        json_kwargs["indent"] = 2
    payload = json.dumps(result, **json_kwargs)

    json_out_path: Optional[Path] = None
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
        json_out_path = args.output
    else:
        print(payload)

    heatmap_requested = args.heatmap or args.heatmap_output is not None
    if heatmap_requested:
        if render_heatmap is None:
            print(
                "Heatmap rendering is unavailable (render_cla_heatmap.py not found).",
                file=sys.stderr,
            )
            return 1
        heatmap_path = args.heatmap_output
        if heatmap_path is None:
            base = json_out_path if json_out_path is not None else Path(args.logfile.name)
            heatmap_path = base.with_suffix(".html")
        heatmap_path.parent.mkdir(parents=True, exist_ok=True)
        title = args.heatmap_title or str(args.logfile)
        html = render_heatmap(result, title=title)
        heatmap_path.write_text(html, encoding="utf-8")
        print(f"Heatmap written to {heatmap_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
