# Hand conversions for `migrate_catalog.py --manual-fixes`

Catalog functions that the migration's rules don't convert (they read a fan-in with
`faasr_get_folder_list`), converted by hand to the plain contract and checked: the old and new
code give the same output on the same inputs (the same figure).

Each file applies only to a code block with that step name AND exactly the old code whose
sha256 it records, so it never touches anything else. The step's input becomes a folder input
(`"kind": "folder"`).

| File | Step | From |
|---|---|---|
| `plot_weather_comparison_8d4832e1.json` | `plot_weather_comparison` | `~/.curate/catalog`, `~/curate-tests/weather` |
| `plot_weather_comparison_4364762f.json` | `plot_weather_comparison` | `~/curate-tests/weather-fresh` |
| `plot_comparison_086b1765.json` | `plot_comparison` | `~/curate-tests/weather-fresh4` |

```bash
uv run --quiet <plugin>/scripts/migrate_catalog.py --catalog-dir <catalog> \
    --manual-fixes <plugin>/migrations/plain-functions            # dry run: report only
uv run --quiet <plugin>/scripts/migrate_catalog.py --catalog-dir <catalog> \
    --manual-fixes <plugin>/migrations/plain-functions --apply    # backup, then write
```

A block no fix matches, and no rule converts, stays `manual` in the report and is not changed.
