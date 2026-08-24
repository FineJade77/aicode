import unittest

from calc import add


class AddTests(unittest.TestCase):
    def test_adds_positive_numbers(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_adds_negative_numbers(self) -> None:
        self.assertEqual(add(-2, -3), -5)


if __name__ == "__main__":
    unittest.main()
