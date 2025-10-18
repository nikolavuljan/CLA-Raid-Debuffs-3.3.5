# guid.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Optional
from data import BOSSES_GUIDS, MULTIBOSSES

def get_guid_type(hex_guid: str) -> Optional[str]:
    """
    0xAABCCCDDDDEEEEEE
      B masked with 0x7 -> 0 player, 1 object, 3 npc, 4 pet, 5 vehicle
    """
    try:
        g = int(hex_guid, 16)
    except Exception:
        return None
    unit_type_mask = (g >> 52) & 0xF
    t = unit_type_mask & 0x7
    return {0: "player", 1: "object", 3: "npc", 4: "pet", 5: "vehicle"}.get(t)

def guid_hex_npc_slice(hex_guid: str) -> str:
    """Return the 6-hex NPC slice (YYYYYY) from 0x0000YYYYYY000000."""
    return hex_guid[6:12] if len(hex_guid) >= 12 else ""

def is_boss_guid(hex_guid: str) -> bool:
    if not hex_guid.startswith("0x"): 
        return False
    npc = guid_hex_npc_slice(hex_guid)
    if npc in BOSSES_GUIDS:
        return True
    for ids in MULTIBOSSES.values():
        if npc in ids:
            return True
    return False

def boss_name_for_guid(hex_guid: str) -> Optional[str]:
    npc = guid_hex_npc_slice(hex_guid)
    # Prefer multiboss group names first (priority)
    for group, ids in MULTIBOSSES.items():
        if npc in ids:
            return group
    return BOSSES_GUIDS.get(npc)

@dataclass
class GuidMapper:
    """
    Stateful mapper that:
      - keeps stable Player/Pet/Vehicle mappings across the whole file
      - (optionally) resets NPC spawned unique suffix per encounter to avoid 'one mob did a billion damage' optics
    """
    # old_hex_guid -> new_fmt_guid
    memo: Dict[str, str] = field(default_factory=dict)
    # optional per-encounter sequence for NPC uniqueness
    npc_counter: int = 1
    # If you detect zone/instance, you can set it from encounters for nicer Creature-... strings
    current_instance_id: int = 0

    def reset_for_new_encounter(self, instance_id: int):
        self.npc_counter = 1
        self.current_instance_id = instance_id

    def convert(self, hex_guid: str, name_field: str = "") -> str:
        if not hex_guid or not hex_guid.startswith("0x"):
            return hex_guid  # already new style or empty/nil
        if hex_guid in self.memo:
            return self.memo[hex_guid]

        gtype = get_guid_type(hex_guid)
        if gtype == "player":
            val = int(hex_guid, 16)
            if val == 0:
                newg = "0000000000000000"
            else:
                # Player-<server>-<uid> ; we just pack low bits as uid
                newg = f"Player-0-{val & 0xFFFFFFFF:08X}"
        elif gtype == "pet":
            npc_hex = guid_hex_npc_slice(hex_guid)
            npc_id = int(npc_hex, 16) if npc_hex else 0
            uniq = f"{self.npc_counter:08X}"; self.npc_counter += 1
            newg = f"Pet-0-0-{self.current_instance_id}-0-{npc_id}-{uniq}"
        elif gtype == "vehicle":
            npc_hex = guid_hex_npc_slice(hex_guid)
            npc_id = int(npc_hex, 16) if npc_hex else 0
            uniq = f"{self.npc_counter:08X}"; self.npc_counter += 1
            newg = f"Vehicle-0-0-{self.current_instance_id}-0-{npc_id}-{uniq}"
        elif gtype in ("npc", "object"):
            npc_hex = guid_hex_npc_slice(hex_guid)
            npc_id = int(npc_hex, 16) if npc_hex else 0
            uniq = f"{self.npc_counter:08X}"; self.npc_counter += 1
            prefix = "Creature" if gtype == "npc" else "GameObject"
            newg = f"{prefix}-0-0-{self.current_instance_id}-0-{npc_id}-{uniq}"
        else:
            # unknown
            newg = hex_guid

        self.memo[hex_guid] = newg
        return newg
