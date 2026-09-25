from __future__ import annotations

import unittest

from variantgrid.event_storage import SQLiteEventRepository

from tests.event_repository_contract import EventRepositoryContractMixin


class SQLiteEventRepositoryContractTests(EventRepositoryContractMixin, unittest.TestCase):
    def make_repository(self) -> SQLiteEventRepository:
        return SQLiteEventRepository()


if __name__ == "__main__":
    unittest.main()
