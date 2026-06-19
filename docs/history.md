# Project History

## Version 1 — Flutter mobile app (graduation project)

The original version of this project was a cross-platform mobile application built with Flutter and shipped as an Android APK. Its purpose was PV site evaluation using an Analytic Hierarchy Process (AHP) weighted-scoring model, grounded in the methodology described in Habib et al. (2020), a study of photovoltaic site suitability on the northwest coast of Egypt. The AHP weight hierarchy from that study informed the criterion structure and relative importance scores used in v1.

The application's main shortcoming was that it required the user to supply all criterion values by hand. For each candidate location, the user had to look up and enter their own geodata — solar irradiation figures, slope values, land-cover class, proximity to roads, and so on — before the app could score and rank the site. There was no automatic data acquisition. As a result, the tool was only as accurate and consistent as the values the user happened to provide, and it placed a significant burden of data collection on non-specialist users. The public repository for v1 contained essentially no source code, making the implementation difficult to reproduce or verify.

## Version 2 — 2026 web rebuild

The 2026 rebuild replaces the mobile app entirely with a web application. The workflow is reversed: the user draws or uploads a polygon area of interest on an interactive map, and the system fetches all required geodata automatically from public APIs — PVGIS (solar resource), Copernicus GLO-30 (terrain/slope), OpenStreetMap/Overpass (roads and infrastructure proximity), ESA WorldCover (land cover), Open-Meteo (climate), and WDPA (protected areas). No manual criterion entry is required or supported.

The analysis engine runs a consistency-checked AHP multi-criteria analysis (Saaty 1980/2008) to derive a Land Suitability Index (LSI) across the area of interest, extracts and ranks discrete candidate sites from the resulting raster, and estimates energy yield for each site using pvlib. Results are presented on the map with a ranked table and can be exported as a PDF report. The full engine is implemented in Python, covered by an automated test suite, and runs in a Docker container, making the analysis reproducible and independently verifiable.

## What carried over, and what changed

The AHP methodology and the original motivating reference (Habib et al. 2020) carried over from v1 to v2. The criterion groupings — solar resource, terrain, land cover, infrastructure proximity, environmental constraints — reflect the same conceptual structure.

Two things did not carry over. First, the deployment target: v1 was a mobile APK; v2 is a containerised web application. Second, the data model: v1 asked users to supply criterion geodata by hand; v2 acquires all geodata automatically from public APIs.

One important caveat on the reference study: the full pairwise comparison matrices and derived weights from Habib et al. (2020) are not publicly available. The rebuild therefore uses documented MCDA default weights validated against the Saaty consistency criterion (CR ≤ 0.10), and does not claim to reproduce the exact numerical weights from the paper.
