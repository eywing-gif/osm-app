"""The analyses made from the map data (after an import of an older version, or by hand): road lanes and amenities.

    venv/bin/python -m pipeline.prepare
"""
from . import amenities, roads
from .util import stage


def main():
    stage(0, 85)
    roads.main()
    stage(85, 100)
    amenities.main()


if __name__ == "__main__":
    main()
