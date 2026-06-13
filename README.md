# SolarSiteSelection

**Draw an area on a map, get a defensible PV siting analysis.**

> 🚧 **Rebuild in progress.** This repository is being rebuilt from the ground up as a
> web-based geospatial PV suitability engine: the user draws an area of interest, the
> system fetches all required geodata from public APIs, runs a consistency-checked AHP
> multi-criteria analysis, produces a Land Suitability Index, extracts ranked candidate
> sites, and estimates energy yield with pvlib. Documentation will land in `docs/` as
> components are completed.

## Quickstart (current state)

```bash
uv sync
make check
```
