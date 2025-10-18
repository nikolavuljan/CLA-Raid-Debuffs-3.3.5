# encounters.py
from __future__ import annotations
from typing import List, Optional, Set, Tuple
from timestamp import split_arg0, arg0_to_rel_seconds, LogClock
from guid import is_boss_guid, boss_name_for_guid, get_guid_type
from data import ENCOUNTER_DATA, ENCOUNTER_NAME_ALIAS, get_difficulty_id, COWARDS

def estimate_encounter_end(fight: List[List[str]],
                           encounter_name: str,
                           player_death_count: int) -> int:
    """
    fight: list of parsed rows (each is list of CSV columns, arg0 included at index 0)
    returns index within fight of the event we align ENCOUNTER_END to.
    """
    name_display = ENCOUNTER_NAME_ALIAS.get(encounter_name, encounter_name)
    is_coward = (name_display in COWARDS or encounter_name in COWARDS)

    def event_type_of(row): return split_arg0(row[0])[1]
    def src_guid(row): return row[1] if len(row) > 1 else ""
    def dst_guid(row): return row[4] if len(row) > 4 else ""

    # scan backwards
    for i in range(len(fight)-1, -1, -1):
        evt = event_type_of(fight[i])
        src = src_guid(fight[i]); dst = dst_guid(fight[i])
        if is_coward:
            if player_death_count >= 22:
                # wipe: first player UNIT_DIED from bottom
                if "UNIT_DIED" in evt and get_guid_type(dst) == "player":
                    return i
            else:
                # kill: last damage from the boss
                if ("SPELL_DAMAGE" in evt or "SWING_DAMAGE" in evt) and is_boss_guid(src):
                    return i
        else:
            if player_death_count < 22:
                # normal kill: boss UNIT_DIED preferred, else last boss damage out
                if "UNIT_DIED" in evt and is_boss_guid(dst):
                    return i
                if ("SPELL_DAMAGE" in evt or "SWING_DAMAGE" in evt) and is_boss_guid(src):
                    return i
            else:
                # wipe: last player death
                if "UNIT_DIED" in evt and get_guid_type(dst) == "player":
                    return i

    # fallback: last event
    return len(fight) - 1

def detect_encounters(rows: List[List[str]]) -> List[Tuple[int,int,str,int,int,int]]:
    """
    Returns a list of encounters as tuples:
      (start_idx, end_idx, encounter_name, encounter_id, instance_id, raid_size)

    We use the 30s 'no boss event' gap rule, players >12 => 25m, else 10m.
    """
    clock = LogClock()
    inside = False
    start_idx = None
    last_boss_time = None
    enc_name = None
    player_guids: Set[str] = set()
    player_deaths = 0
    out: List[Tuple[int,int,str,int,int,int]] = []

    for i, row in enumerate(rows):
        parts = row
        arg0 = parts[0]
        calendar, evt = split_arg0(arg0)
        t = arg0_to_rel_seconds(arg0, clock)
        src = parts[1] if len(parts)>1 else ""
        dst = parts[4] if len(parts)>4 else ""

        if not inside:
            if ("SWING_DAMAGE" in evt or "SPELL_DAMAGE" in evt) and (is_boss_guid(src) or is_boss_guid(dst)):
                inside = True
                start_idx = i
                enc_name = boss_name_for_guid(src) if is_boss_guid(src) else boss_name_for_guid(dst)
                last_boss_time = t
                # players
                if len(parts)>1 and get_guid_type(src) == "player": player_guids.add(src)
                if len(parts)>4 and get_guid_type(dst) == "player": player_guids.add(dst)
        else:
            # still in encounter
            if (is_boss_guid(src) or is_boss_guid(dst)):
                # refresh activity timer if same encounter family
                # (both single and multiboss resolve to group/boss name)
                name_here = boss_name_for_guid(src) or boss_name_for_guid(dst)
                if name_here == enc_name:
                    last_boss_time = t
            if "UNIT_DIED" in evt and len(parts)>4 and get_guid_type(dst) == "player":
                player_deaths += 1
            if len(parts)>1 and get_guid_type(src) == "player": player_guids.add(src)
            if len(parts)>4 and get_guid_type(dst) == "player": player_guids.add(dst)

            if last_boss_time is not None and (t - last_boss_time) > 30:
                # evaluate the buffered fight window
                end_idx = estimate_encounter_end(rows[start_idx:i+1], enc_name, player_deaths) + start_idx
                name_display = ENCOUNTER_NAME_ALIAS.get(enc_name, enc_name)
                info = ENCOUNTER_DATA.get(name_display, {"encounter_id": 0, "instance_id": 0})
                raid_size = 25 if len(player_guids) > 12 else 10
                out.append((start_idx, end_idx, name_display, info["encounter_id"], info["instance_id"], raid_size))
                # reset
                inside = False
                start_idx = None
                last_boss_time = None
                enc_name = None
                player_guids.clear()
                player_deaths = 0

    # if ended mid-fight, close it at the last line as a wipe-ish finish
    if inside and start_idx is not None:
        end_idx = estimate_encounter_end(rows[start_idx:], enc_name, player_deaths) + start_idx
        name_display = ENCOUNTER_NAME_ALIAS.get(enc_name, enc_name)
        info = ENCOUNTER_DATA.get(name_display, {"encounter_id": 0, "instance_id": 0})
        raid_size = 25 if len(player_guids) > 12 else 10
        out.append((start_idx, end_idx, name_display, info["encounter_id"], info["instance_id"], raid_size))

    return out
