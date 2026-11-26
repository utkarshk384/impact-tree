from collections import defaultdict
from itertools import combinations
import unittest

from test_analysis import impact
from canvas_fixture import fixture


class CanvasLayoutTest(unittest.TestCase):
    def setUp(self):
        dependencies = fixture()
        def score(path):
            if "/stage-" in path:
                return 90 + int(path[-4])
            return 100 - len(path)

        self.files = [{"path": path, "score": score(path), "usedBy": []} for path in dependencies]
        self.layout = impact.place_tree(self.files, dependencies)
        self.positions = self.layout["positions"]

    def test_fixture_has_five_columns_and_no_overlapping_cards(self):
        self.assertEqual(len(self.files), 120)
        self.assertEqual(self.layout["columns"], 5)
        self.assertEqual(self.layout["columnStep"] - self.layout["cardWidth"], 120)
        width, height = self.layout["cardWidth"], self.layout["cardHeight"]
        for first, second in combinations(self.positions.values(), 2):
            self.assertTrue(abs(first["x"] - second["x"]) >= width or
                            abs(first["y"] - second["y"]) >= height,
                            (first, second))

    def test_children_are_in_parents_bands_and_parents_are_centered(self):
        children = defaultdict(list)
        for file in self.files:
            if file["parent"] is not None:
                children[file["parent"]].append(self.positions[file["path"]]["band"])
        self.assertEqual(max(map(len, children.values())), 15)
        for parent, bands in children.items():
            position = self.positions[parent]
            top, bottom = position["band"]
            self.assertTrue(all(top <= a < b <= bottom for a, b in bands))
            self.assertEqual(position["y"] + self.layout["cardHeight"] / 2, (top + bottom) / 2)
            for first, second in zip(sorted(bands), sorted(bands)[1:]):
                self.assertGreaterEqual(second[0] - first[1], 24)

    def test_standalone_cards_fill_a_grid_below_the_tree(self):
        standalone = [self.positions[file["path"]] for file in self.files if file["standalone"]]
        self.assertEqual(len(standalone), 10)
        self.assertEqual(len({position["x"] for position in standalone}), 5)
        self.assertEqual(len({position["y"] for position in standalone}), 2)
        self.assertTrue(all(position["y"] > self.layout["treeBottom"] for position in standalone))

    def test_labels_keep_extensions_and_the_shortest_unique_path(self):
        names = {file["path"]: file["cardName"] for file in self.files}
        self.assertEqual(names["old/layout.css"], "old/layout.css")
        self.assertEqual(names["css/layout.css"], "css/layout.css")
        self.assertEqual(names["legacy/diagram.js"], "diagram.js")
        self.assertEqual(names["public/diagram.css"], "diagram.css")
        self.assertEqual(names["api/users/route.ts"], "users/route.ts")
        self.assertEqual(names["api/teams/route.ts"], "teams/route.ts")
        self.assertEqual(names["pages/search.tsx"], "search.tsx")
        self.assertEqual(names["utilities/helper-00.ts"], "helper-00")
        self.assertEqual(len(set(names.values())), len(names))
        self.assertEqual(impact.card_labels(["route.ts", "x/route.ts", "y/x/route.ts"]),
                         {"route.ts": "./route.ts", "x/route.ts": "x/route.ts",
                          "y/x/route.ts": "y/x/route.ts"})
        self.assertEqual(impact.card_labels(["config.dev", "config.dev.ts"]),
                         {"config.dev": "config.dev", "config.dev.ts": "config.dev.ts"})
