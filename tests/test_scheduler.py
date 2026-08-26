import asyncio
import unittest

from farm import FarmScheduler


class ReservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_is_atomic(self):
        scheduler = FarmScheduler(None, None, None)  # reservation logic has no external dependencies
        first, second = await asyncio.gather(
            scheduler._reserve([1, 2]), scheduler._reserve([1, 2])
        )
        self.assertEqual(sorted(first + second), [1, 2])
        await scheduler._release([1, 2])
        self.assertEqual(await scheduler._reserve([1, 2]), [1, 2])
