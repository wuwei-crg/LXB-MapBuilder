# LXB-MapBuilder

English | [中文](docs/zh/lxb_map_builder.md)

This repository is the standalone home for map construction assets:
- `map_builder/`: builder code migrated from `LXB-Framework/src/auto_map_builder`
- `maps/`: map data snapshots for development and publishing
- `docs/`: map builder documentation
- `web_console/`: map builder web UI (migrated from `LXB-Framework/web_console`)

## Related Repositories

- Runtime framework (Android FSM + scheduler): https://github.com/wuwei-crg/LXB-Framework
- Canonical map artifacts repository: https://github.com/wuwei-crg/LXB-MapRepo

## Run Web Console

```bash
cd web_console
python app.py
```

Main page: `http://localhost:5000/`

## Migration Note

Initial content was split from `LXB-Framework` on 2026-03-19.

## Next Steps

1. Add `tools/publish_to_maps_repo.ps1` to publish maps to `LXB-Maps`.
2. Add schema + validation workflow for PR checks.
3. Define release/version policy for map packages.
