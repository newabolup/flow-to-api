"""flowbridge: Google Flow web UI -> local HTTP API.

Package layout
--------------
config      env-driven settings (one object, no globals)
state       atomic state.json (project URL, model cache)
driver      the only Playwright adapter (probe / wait / click)
ui          Flow UI recipe: selectors, settings, prompt, result, download
image_flow  generate + edit orchestration
video_flow  text-to-video + video-edit orchestration
discovery   model menu scraping (memory + disk cache)
runtime     browser ownership (persistent context, idle release)
jobs        bounded job queue with event-driven results
api         FastAPI routes
diag        opt-in diagnostics
"""
__version__ = "3.0.0"
