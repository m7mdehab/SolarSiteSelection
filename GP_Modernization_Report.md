# Graduation Project Modernization Report
## Oil Spill Detection & Solar Site Selection — Honest Assessment and Upgrade Plan

**Prepared:** June 2026
**Scope:** Both repositories (`m7mdehab/oil-spill-detection`, `m7mdehab/SolarSiteSelection`), the Semester 1 & 2 reports, the final presentation, and the deployed artifacts.

---

## 1. Executive Summary

These two projects were genuinely good graduation work for their time: a four-model semantic segmentation comparison on Sentinel-1 SAR imagery with a deployed Streamlit demo, and a cross-platform Flutter app implementing a weighted scoring model for PV site evaluation grounded in a real AHP study. As of 2026, however, neither repository would survive a hiring manager's first five minutes. The gap is not primarily about model choice or framework age — it is that the repositories don't demonstrate engineering judgment, the reported metrics contain errors that an experienced reviewer would catch immediately, and in the solar project's case, the public repository contains essentially no code at all.

The good news is that the underlying domain (geospatial ML for environmental monitoring and renewable energy) has become *more* fashionable since you graduated, not less. Earth-observation foundation models, cloud-native geospatial data access, and energy modeling tooling have all matured dramatically. A rebuild of these two projects against the 2026 bar would produce a portfolio that reads as "experienced remote-sensing ML engineer" rather than "student who completed a capstone." This report covers what exists today, what is wrong with it at three levels (credibility, engineering, and product), and a concrete, phased plan to rebuild both.

---

## 2. Project One: Oil Spill Detection

### 2.1 What exists today

The repository is a multi-page Streamlit app generated from the `streamlit-hello` template. It loads four trained segmentation models — U-Net, FCN, and SegNet as Keras `.h5` files committed directly to git, and DeepLabV3+ as a TFLite file tracked with Git LFS — and lets a user upload a SAR image or pick one of five samples, run inference, and view a color-coded class mask with per-class pixel percentages. The training itself happened in Google Colab notebooks that were never committed; the dataset is the well-known Sentinel-1 oil spill segmentation dataset (5 classes: sea surface, oil spill, look-alike, ship, land) at 256×256.

### 2.2 Credibility problems — these matter most

These are the issues a technically experienced reviewer notices first, and they damage trust in the whole project more than any stale dependency could.

**The headline metrics are misleading, and the report's own confusion matrix proves it.** The reported test accuracy of 93–96% is pixel accuracy on a dataset where sea surface dominates the image area. Reading the U-Net confusion matrix in your own Semester 2 report: of ~77,000 true oil-spill pixels, only ~42,000 were predicted correctly — roughly 54% recall on the class the entire project is named after. The ship class was detected **zero** times (886 true ship pixels, 0 predicted as ship). A model can fail almost completely at finding oil and ships and still post 93% pixel accuracy because the ocean is big. Any practitioner reviewing this will see "95.84% accuracy" next to that confusion matrix and conclude the authors didn't understand their own evaluation.

**Accuracy, precision, and recall are reported as identical numbers for three of the four models.** FCN: 93.23 / 93.23 / 93.23. SegNet: 92.87 / 92.87 / 92.87. DeepLabV3+: 95.84 / 95.84 / 95.84. This happens when you compute micro-averaged precision/recall over a multiclass problem — they mathematically collapse to accuracy and add zero information. Reporting all three as separate columns signals that the metrics were taken from a library default without understanding what they measure. The correct presentation is per-class precision/recall/IoU plus macro averages.

**The IoU column is internally inconsistent.** U-Net is reported at 40.0% IoU while FCN is at 87.3% — yet U-Net has *higher* accuracy and precision than FCN. An 87% mIoU on this dataset would be near state-of-the-art (published SOTA on this exact dataset hovers around 65–70% mIoU); a 40% next to it in the same table strongly suggests the four notebooks computed IoU differently (binary vs. multiclass, mean vs. weighted, with vs. without background). Inconsistent measurement across the very models being compared undermines the comparison, which is the project's central claim.

**Numbers disagree between the report and the app.** The Model Comparisons page in the app reports DeepLabV3+ at 96.25% accuracy / 92.77% IoU; the Semester 2 report says 95.84% / 92.01%. Small discrepancy, but discrepancies between your own published artifacts are exactly what reviewers screen for.

**The training code does not exist publicly.** There are no training notebooks, no scripts, no configs, no seeds, no data-loading code in the repo. None of the results are reproducible. In 2026, "trust me, the Colab notebook said so" is not a portfolio artifact.

### 2.3 Engineering problems

**The deployed DeepLabV3+ model is broken.** `deeplab_model.tflite` in the repo is a 133-byte Git LFS pointer file, not the 55 MB model. Anyone who clones the repo without LFS configured — and any deployment that doesn't pull LFS objects — gets a crash on import, because `model_functions.py` loads all four models *at module import time*. The recommended default model in the app is the one most likely to be broken.

**All four models load eagerly at import, with no caching.** There is no `st.cache_resource`; on Streamlit's execution model this means slow cold starts and unnecessary memory pressure from holding four full models (~75 MB of weights, much more in RAM) when the user will only ever use one at a time.

**The progress bar is fake.** A `time.sleep(0.001)` loop animates 0→100% *before* inference even begins, then prediction runs after the bar completes. Cosmetic, but it's the kind of detail that reads as a student demo.

**`predict()` returns `None` and the result is assigned to a variable anyway.** `process_predictions()` renders Streamlit UI directly (mixing inference logic with presentation) and returns nothing; the calling page does `label = model_functions.predict(...)` and ignores that `label` is always `None`. There's also a dangling `if __name__ == "__main__": main()` in `model_functions.py` that references a function that doesn't exist in that file.

**Repository hygiene.** `__pycache__/` is committed. The leftover `utils.py` from the Streamlit hello template (with Snowflake's copyright header) is still in the tree. The README is the template boilerplate ("Python 3.6 or later" is the entire prerequisites section). There's no license, no tests, no CI, no Dockerfile, no pinned dependency versions except TensorFlow, no `.gitignore` entries for what actually needed ignoring. And most strangely: **the solar project's 24 MB compiled APK lives in the oil spill repo**, presumably because it needed somewhere public to host the download link. Model weights and app binaries don't belong in git at all — that's what Hugging Face Hub and GitHub Releases are for.

### 2.4 Product problems

The app accepts a JPEG/PNG and returns a 256×256 color mask, full stop. There is no overlay on the original image, no preservation of original resolution, no GeoTIFF support, no geolocation of the detection, no estimated spill area in km², no confidence/probability map, and no way to process more than one image. A real oil-spill workflow starts from a Sentinel-1 scene (a GeoTIFF with coordinates), not a screenshot — and the output that matters is *where* the spill is and *how big* it is, not which pixels in an anonymous crop are cyan. The current app demonstrates that the model runs; it does not demonstrate that you understand the operational problem.

### 2.5 What the 2026 version looks like

**Modeling.** Move to PyTorch (the field has consolidated there) and benchmark against the modern baseline set: keep one classic encoder-decoder (U-Net with a pretrained backbone via `segmentation-models-pytorch`) as the reference, then add a transformer model (SegFormer or Mask2Former via Hugging Face) and — this is the genuinely differentiating piece — a fine-tuned **Earth-observation foundation model** (Prithvi-EO 2.0, Clay, or DOFA) or a SAM2-based approach. Handle the class imbalance explicitly with Dice + Focal loss and report it as a deliberate design decision. Use SAR-appropriate augmentation (no color jitter on radar backscatter; speckle-aware noise instead).

**Evaluation that survives scrutiny.** Per-class IoU, mIoU, per-class F1, precision-recall curves for the oil class, and a confusion matrix you discuss honestly. Add the experiment that proves real-world competence: take a *known historical spill event* never seen in training, pull the raw Sentinel-1 scene yourself, run your full pipeline end-to-end, and show the geocoded detection next to the documented spill extent. One real-event case study is worth more than ten points of benchmark accuracy.

**A real pipeline, not an image uploader.** Build the ingest path: query the Copernicus Data Space (or ASF) API for Sentinel-1 GRD scenes over an AOI and date range → radiometric calibration, speckle filtering, dB conversion (pyroSAR or a thin SNAP wrapper, mirroring the preprocessing your report already describes) → tiling → batched inference → stitching → output as a geocoded GeoTIFF mask **and** vectorized spill polygons with computed area in km². This is the single biggest upgrade: it converts "a model demo" into "an environmental monitoring system."

**Serving and reproducibility.** Export the chosen model to ONNX; serve via FastAPI with ONNX Runtime; containerize with Docker; host weights on Hugging Face Hub with a proper model card; track training runs with Weights & Biases or MLflow; version the dataset reference with DVC or a download script with checksums; full training code with configs (Hydra or plain YAML) and seeds. Frontend: either a modernized Streamlit/Gradio app on HF Spaces with a map view (folium/leafmap) showing detections in their geographic context, or a small React + MapLibre frontend if you want to flex full-stack range.

**Engineering baseline.** Pytest suite (preprocessing invariants, mask encoding round-trips, API contract tests), GitHub Actions (lint with ruff, type-check with mypy/pyright, run tests, build the Docker image), pre-commit hooks, `pyproject.toml` with locked dependencies (uv), a README with an architecture diagram, demo GIF, honest results table, and reproduction instructions that actually work from a fresh clone.

---

## 3. Project Two: Solar Site Selection

### 3.1 What exists today

This is the harder conversation. The public `SolarSiteSelection` repository contains a one-line README, an empty `app.py` (literally one byte), and a `requirements.txt` listing geopandas, rasterio, scikit-learn and friends — dependencies for an application that was never written. The actual deliverable was a Flutter mobile app, distributed only as a compiled APK committed to the oil-spill repo. Its source code is not public anywhere.

Functionally, the app is a questionnaire: the user manually selects ranges from dropdowns (irradiance bracket, slope bracket, distance-to-roads bracket, etc.), the app multiplies selections by hardcoded weights derived from the Habib et al. (2020) AHP study, outputs a "success percentage," optionally compares two sites, and generates a report document.

### 3.2 The honest assessment

**The empty repo is actively hurting you.** A recruiter who clicks through from your CV to a repository containing a blank `app.py` concludes worse things than if the repo didn't exist. Either populate it or take it down — today, independent of everything else in this report.

**The shipped app abandoned the documented methodology.** Your Semester 1 report promised GIS-MCDA integration, Fuzzy-AHP/ANP comparison, NDVI-based exclusion masking, suitability map generation, and PVOUT calculation. The shipped app contains none of that: no GIS, no raster analysis, no maps, no fuzzy logic, no PVOUT — just a static weighted-sum form. I understand exactly how that happens under capstone deadlines, but the result is that the most intellectually substantial 70% of the project exists only as a literature review.

**The app's core design defeats its own purpose.** It asks the *user* to already know their site's solar irradiance, slope, aspect, elevation, distance to transmission infrastructure, and land cover. Anyone who has all of that data has already done the site assessment; the tool adds a multiplication. Meanwhile, every one of those inputs is freely available from public APIs given nothing but a coordinate. The single most important product insight for the rebuild: **the user should provide a location, and the system should provide the data.**

**Binary-only distribution is a portfolio dead end.** An APK demonstrates nothing reviewable. Nobody is going to sideload an Android app from a GitHub raw link to evaluate your code quality — and in 2026, security-conscious reviewers actively won't.

### 3.3 What the 2026 version looks like

This project has the higher ceiling of the two, because the rebuilt version can be something that genuinely doesn't exist as a polished open tool: an end-to-end, interactive PV site suitability engine. The user experience to aim for:

**The user draws a polygon on a map (or picks a region of Egypt), and the system does everything else.** Behind that interaction: fetch solar resource data from PVGIS or the Global Solar Atlas API; pull elevation from SRTM/Copernicus DEM (OpenTopography API) and derive slope and aspect with `xarray`/`richdem`; query OpenStreetMap via Overpass for roads, transmission lines, and urban areas, then compute proximity rasters; pull land cover from ESA WorldCover and protected areas from WDPA for exclusion masking. Every layer your methodology chapter described, acquired automatically.

**Implement the AHP properly and make it interactive.** Reuse the pairwise comparison structure and weights from the Habib et al. study as defaults, but let the user adjust the pairwise judgments with sliders and recompute the weight vector and **Consistency Ratio live** — rejecting inconsistent matrices (CR > 10%) exactly as the methodology prescribes. This turns the decision-science content of your report into working, demonstrable code, and it's a feature that makes domain experts smile.

**Produce the real outputs.** A reclassified Land Suitability Index raster over the AOI (most suitable → restricted, the five classes from the literature), rendered as an interactive map layer (leafmap / folium / MapLibre); automatic extraction of the top-k candidate sites as polygons; and for each candidate, an energy yield estimate computed with **pvlib** (the industry-standard library — using it signals domain fluency), plus a simple LCOE figure so the output speaks the language of an actual developer. Finish with a one-click PDF report export, which preserves the one genuinely nice feature of the original Flutter app.

**Stack.** Python throughout: rasterio/rioxarray + geopandas + xarray for the geospatial core, pvlib for energy modeling, FastAPI backend (or Streamlit if you want speed-to-demo, but the FastAPI + lightweight map frontend version reads as more senior), Docker, tests on the analytical core (AHP math, raster reclassification, CR computation — all highly unit-testable), CI, and a written-up validation: run your tool over the same Northwest Coast study area as the reference paper and compare your suitability map against theirs. Quantified agreement with a peer-reviewed study is a spectacular README section.

If you still want a mobile artifact, make it a thin client of the same backend — but the web tool is the portfolio piece.

---

## 4. Cross-Cutting: Portfolio-Level Presentation

A few things apply to both projects regardless of technical content. Each repo needs a README that functions as a landing page: one-paragraph problem statement, an animated demo GIF within the first screenful, an architecture diagram, an honest results table with per-class metrics, a quickstart that works (`docker compose up` or `uv sync && make demo`), and a limitations section — explicitly stating what the model gets wrong is a seniority signal, not a weakness. Both repos need licenses, both need the binary artifacts (model weights, APK) moved out of git history entirely (use `git filter-repo`, then host weights on Hugging Face Hub and binaries on GitHub Releases), and both need their concerns separated — the solar APK leaves the oil-spill repo. Finally, fix or remove dead links: if `oil-spill-detector.streamlit.app` no longer resolves, every document pointing at it is a broken promise; redeploy on HF Spaces or Streamlit Cloud and update everything.

The framing matters too. As graduation work, the two halves were bundled as "Eco-Mapping." As portfolio work, present them as two independent, complete systems — "SAR-based marine oil spill monitoring pipeline" and "Geospatial PV site suitability engine" — each with its own narrative arc of problem → system → validation → limitations. Two focused systems read as far stronger than one umbrella project.

---

## 5. Prioritized Roadmap

**Phase 0 — Stop the bleeding (a weekend).** Take down or stub the empty SolarSiteSelection repo with a "rebuild in progress" README. Fix the broken LFS pointer or remove the dead DeepLabV3+ path from the oil-spill app. Delete `__pycache__`, the template `utils.py`, and the APK from the oil-spill repo. Write real READMEs. Reconcile the metric discrepancies between the report and the app, or annotate them.

**Phase 1 — Oil spill credibility (2–3 weeks).** Reconstruct or rewrite the training code in PyTorch and commit it. Re-evaluate all models with a single, consistent metric implementation; publish per-class IoU and an honest discussion of oil/ship class performance. Add `st.cache_resource`, remove the fake progress bar, separate inference from UI, add tests and CI. Move weights to HF Hub.

**Phase 2 — Oil spill modernization (4–6 weeks).** Add SegFormer/Mask2Former and one EO foundation model fine-tune with imbalance-aware losses. Build the Sentinel-1 ingest → preprocess → tile → infer → stitch → geocode pipeline with GeoTIFF/polygon outputs and area estimation. Run the real-event case study. Redeploy with a map-based frontend.

**Phase 3 — Solar rebuild (4–6 weeks).** Build the AOI-driven data acquisition layer, the interactive AHP engine with live consistency checking, the weighted-overlay LSI raster pipeline, pvlib yield + LCOE estimation, the interactive map, and PDF export. Validate against the Habib et al. study area and write up the comparison.

**Phase 4 — Polish (1–2 weeks).** Demo GIFs, architecture diagrams, model cards, a short technical blog post per project, and a one-page portfolio site linking both.

Done in full, this takes two student capstones and turns them into the two strongest kinds of portfolio evidence that exist: a deployed ML system evaluated honestly against real-world events, and a domain tool that practitioners could actually use.
