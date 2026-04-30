"""Arrow IPC buffer tools — placeholder.

Source: ``the original plugin/lib/tools/buffers-arrow.js`` (775 lines, the
heaviest tool module). Provides:

* ``arrow_buffer_summary`` — schema + sample + row count from an Arrow IPC handle.
* ``arrow_buffer_to_3d_scatter`` / ``…_animated`` — three.js scatter plots.
* ``arrow_buffer_to_2d_plot``                      — Chart.js 2D plot.
* ``arrow_buffer_query``                            — small filtered row sample.

All of these depend on the frontend buffer store + arrow.iife.min.js +
Three.js bundle. Port the frontend buffer infrastructure first, then come
back here. The Python side will mostly be a pass-through to the browser
primitive that does the actual decoding/rendering.
"""

# No tools registered yet — see docs/04-tool-catalog.md.
