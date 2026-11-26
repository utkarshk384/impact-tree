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
                children[file["parent"]].append(self.positions[file["path"]])
        self.assertEqual(max(map(len, children.values())), 15)
        for parent, positions in children.items():
            position = self.positions[parent]
            top, bottom = position["band"]
            by_x = defaultdict(list)
            for child in positions:
                start, end = child["band"]
                self.assertLess(position["x"], child["x"])
                self.assertTrue(top <= start < end <= bottom)
                by_x[child["x"]].append(child["band"])
            self.assertEqual(position["y"] + self.layout["cardHeight"] / 2, (top + bottom) / 2)
            for bands in by_x.values():
                ordered = sorted(bands)
                for first, second in zip(ordered, ordered[1:]):
                    self.assertGreaterEqual(second[0] - first[1], 24)

    def test_dense_leaf_groups_and_childless_trunk_files_wrap(self):
        hub = "features/feature-0/shared.ts"
        children = [self.positions[file["path"]] for file in self.files if file["parent"] == hub]
        trunk_leaves = [position for position in self.positions.values()
                        if position["column"] == 0 and "gridLane" in position]
        self.assertEqual(len(trunk_leaves), 42)
        self.assertTrue(all(position["y"] > self.layout["branchBottom"] for position in trunk_leaves))
        self.assertEqual(len(children), 15)
        for positions in (children, trunk_leaves):
            self.assertGreater(len({position["x"] for position in positions}), 1)
            self.assertLessEqual(len({position["y"] for position in positions}), 8)
            self.assertTrue(all("gridLane" in position for position in positions))

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
        self.assertEqual(names["features/feature-0/shared.ts"], "feature-0/shared.ts")
        self.assertEqual(names["features/feature-0/views/view-05.ts"], "feature-0/views/view-05.ts")
        self.assertEqual(len(set(names.values())), len(names))
        self.assertEqual(impact.card_labels(["route.ts", "x/route.ts", "y/x/route.ts"]),
                         {"route.ts": "./route.ts", "x/route.ts": "x/route.ts",
                          "y/x/route.ts": "y/x/route.ts"})
        self.assertEqual(impact.card_labels(["config.dev", "config.dev.ts"]),
                         {"config.dev": "config.dev", "config.dev.ts": "config.dev.ts"})
