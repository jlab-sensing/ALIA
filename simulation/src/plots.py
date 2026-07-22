"""Central path resolver for every generated figure, so plots never collide
and the output tree stays self-describing. Layout: <output>/plots/<category>/
<name>.png (categories: overlay, histogram, sweep)."""

import os


def plot_path(plots_dir: str, category: str, name: str) -> str:
    """Return `<plots_dir>/<category>/<name>.png`, creating the category dir.
    The ".png" suffix is added if absent."""
    category_dir = os.path.join(plots_dir, category)
    os.makedirs(category_dir, exist_ok=True)
    if not name.endswith(".png"):
        name += ".png"
    return os.path.join(category_dir, name)
