import os, random, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br import pack


def brute(ms):
    """Exact optimum of max load + 2000*nonempty over <=8 workers (set partitions)."""
    n = len(ms)
    best = [10**18]
    loads = []

    def rec(i):
        if i == n:
            c = max(loads) + 2000 * len(loads)
            if c < best[0]:
                best[0] = c
            return
        for b in range(len(loads)):
            loads[b] += ms[i]
            rec(i + 1)
            loads[b] -= ms[i]
        if len(loads) < 8:
            loads.append(ms[i])
            rec(i + 1)
            loads.pop()

    rec(0)
    return best[0]


def gen(rnd, n):
    kind = rnd.random()
    if kind < 0.4:
        return [rnd.randint(1, 5000) for _ in range(n)]
    if kind < 0.7:
        return [rnd.choice([1, 2, 3, 30, 800, 3000, 9000, 28000]) for _ in range(n)]
    return [int(rnd.expovariate(1 / 1500)) + 1 for _ in range(n)]


class T(unittest.TestCase):
    def test_empty_and_single(self):
        self.assertEqual(pack.best([]), [])
        self.assertEqual(pack.best([(7, 100)]), [[7]])

    def test_valid_partition(self):
        rnd = random.Random(1)
        tests = [(i, d) for i, d in enumerate(gen(rnd, 300))]
        w = pack.best(tests)
        self.assertLessEqual(len(w), 8)
        self.assertEqual(sorted(t for x in w for t in x), list(range(300)))
        self.assertTrue(all(x for x in w))

    def test_cost_ms(self):
        d = {1: 0.0005, 2: 1.2344, 3: 0.0015}
        self.assertEqual(pack.cost_ms([], d), 0)
        self.assertEqual(pack.cost_ms([[]], d), 0)
        # round half up: 0.5 -> 1, 1234.4 -> 1234, 1.5 -> 2
        self.assertEqual(pack.cost_ms([[1], [2, 3]], d), (1234 + 2) + 4000)

    def test_vs_bruteforce(self):
        rnd = random.Random(42)
        total = opt = 0
        worst = 0
        for _ in range(600):
            n = rnd.randint(1, 9)
            ms = gen(rnd, n)
            tests = list(enumerate(ms))
            c = pack.cost_of_ms(pack.best(tests, exact=False), dict(tests))
            c2 = pack.cost_of_ms(pack.best(tests), dict(tests))
            b = brute(ms)
            self.assertGreaterEqual(c, b)
            self.assertEqual(c2, b)  # with the exact stage on small n we must be optimal
            total += 1
            opt += c == b
            worst = max(worst, c - b)
        print(f"\nheuristic-only optimal in {opt}/{total}; worst gap {worst} ms")

    def test_speed_1620(self):
        rnd = random.Random(3)
        ms = [max(1, int(rnd.expovariate(1 / 64))) for _ in range(1620)]
        ms[:3] = [28800, 21500, 6700]
        tests = list(enumerate(ms))
        t = time.perf_counter()
        for _ in range(20):
            pack.best(tests)
        per = (time.perf_counter() - t) / 20 * 1000
        print(f"\nbest() on 1620 tests: {per:.2f} ms")
        self.assertLess(per, 5)


if __name__ == "__main__":
    unittest.main()
