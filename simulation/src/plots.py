"""
@file       plots.py

One place that decides where every generated graph in the project is written,
so figures never collide and the output tree stays self-describing.

Layout under the run's output directory:

    <output>/plots/
        overlay/<logger>.png       raw-vs-filtered time series (compare.py)
        histogram/<logger>.png     density histogram of raw values (compare.py)
        sweep/<name>.png           tradeoff curves (sweep.py)

Both CLI entry points route through `plot_path()` so a new plot type only needs
a new category name, not a new ad-hoc path convention.
"""

import os


def plot_path(plots_dir: str, category: str, name: str) -> str:
    """Return `<plots_dir>/<category>/<name>.png`, creating the category dir.

    `category` groups related figures (e.g. "overlay", "histogram", "sweep");
    `name` identifies the individual figure within that group (a logger name,
    a metric name, ...). The ".png" suffix is added if absent.
    """
    category_dir = os.path.join(plots_dir, category)
    os.makedirs(category_dir, exist_ok=True)
    if not name.endswith(".png"):
        name += ".png"
    return os.path.join(category_dir, name)
