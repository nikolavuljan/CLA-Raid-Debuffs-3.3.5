# WoTLK CLA Parser & Heatmap

Utilities for analysing Wrath of the Lich King (3.3.5) combat logs and reporting raid debuff coverage in the same structure as the Google Sheets CLA workbook.

<img width="1744" height="667" alt="image" src="https://github.com/user-attachments/assets/23e1d568-6c5d-4e73-8c5d-5c0f4ba817b2" />



## Prerequisites

- Python 3.8+

## Usage

### Generate JSON Coverage

```bash
python3 cla_parser.py path/to/WoWCombatLog.txt --pretty -o output.json
```

Options:
- `--heatmap` – also renders an HTML heatmap (defaults to `output.html` next to the JSON).
- `--heatmap-output <path>` – custom destination for the HTML heatmap.
- `--heatmap-title "Custom Title"` – override the HTML page title.

### One-Step JSON + Heatmap Example

```bash
python3 cla_parser.py logs/WoWCombatLog.txt --pretty --heatmap -o reports/cla_report.json
```

This writes:
- `reports/cla_report.json`
- `reports/cla_report.html`

### Customising Debuff Mapping

On the first run the parser generates `cla_map.json`. Edit this file to:
- Add spell aliases for non-English client logs (`aliases` arrays).
- Remove lower ranks (keep the highest rank per spell name).
- Extend with new categories if needed.

Re-run `cla_parser.py` after editing to apply your changes.
