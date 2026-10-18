"""定义厂商无关 ObjectStore 契约测试及仅供测试的内存替身。"""

from __future__ import annotations

import unittest

from sec.exceptions import ObjectNotFoundError, ObjectStoreError
from sec._types.objects import StoredObject
from sec._types.storage import ObjectStore


class ObjectStoreContract:
    """验证对象写入覆盖、读取、缺失处理、幂等删除和关闭语义。"""

    async def make_store(self) -> ObjectStore:
        raise NotImplementedError

    async def test_put_get_and_overwrite(self) -> None:
        store = await self.make_store()

        await store.put("assets/item", b"first", content_type="text/plain")
        stored = await store.get("assets/item")
        self.assertEqual(stored.data, b"first")
        self.assertEqual(stored.content_type, "text/plain")
        await store.put("assets/item", b"second")
        stored = await store.get("assets/item")
        self.assertEqual(stored.data, b"second")
        self.assertIsNone(stored.content_type)

    async def test_missing_get_raises_not_found(self) -> None:
        store = await self.make_store()

        with self.assertRaises(ObjectNotFoundError):
            await store.get("missing")

    async def test_delete_is_idempotent(self) -> None:
        store = await self.make_store()

        await store.delete("missing")
        await store.put("assets/item", b"payload")
        await store.delete("assets/item")
        await store.delete("assets/item")

        with self.assertRaises(ObjectNotFoundError):
            await store.get("assets/item")

    async def test_close_releases_store_and_rejects_operations(self) -> None:
        store = await self.make_store()
        await store.aclose()

        with self.assertRaises(ObjectStoreError):
            await store.get("key")
        with self.assertRaises(ObjectStoreError):
            await store.put("key", b"value")
        with self.assertRaises(ObjectStoreError):
            await store.delete("key")


class InMemoryObjectStoreDouble:
    """实现 ObjectStore 契约的测试替身，不属于生产存储后端。"""

    def __init__(self) -> None:
        self._objects: dict[str, StoredObject] = {}
        self._closed = False

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        self._ensure_open()
        if not isinstance(key, str) or not key.strip():
            raise ValueError("object key must not be empty")
        if not isinstance(data, bytes):
            raise TypeError("object data must be bytes")
        if content_type is not None and (
            not isinstance(content_type, str) or not content_type.strip()
        ):
            raise ValueError("content type must not be empty")
        self._objects[key] = StoredObject(data, content_type)

    async def get(self, key: str) -> StoredObject:
        self._ensure_open()
        try:
            return self._objects[key]
        except KeyError as exc:
            raise ObjectNotFoundError(f"object {key!r} was not found") from exc

    async def delete(self, key: str) -> None:
        self._ensure_open()
        self._objects.pop(key, None)

    async def aclose(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ObjectStoreError("object store is closed")


class ObjectStoreContractTests(
    ObjectStoreContract,
    unittest.IsolatedAsyncioTestCase,
):
    """在测试替身上运行 ObjectStore 公共契约测试。"""

    async def make_store(self) -> ObjectStore:
        return InMemoryObjectStoreDouble()


if __name__ == "__main__":
    unittest.main()
