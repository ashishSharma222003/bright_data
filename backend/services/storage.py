import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    """
    Abstract storage interface. Swap LocalStorage for S3Storage without
    changing any calling code — just update the get_storage() factory.
    """

    @abstractmethod
    def save(self, file_obj, key: str) -> str:
        """Persist file_obj under the given key. Returns the storage key."""

    @abstractmethod
    def get_path(self, key: str) -> str:
        """Resolve a storage key to a readable file path or URL."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove a stored file by key."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check whether a key exists in storage."""


class LocalStorage(StorageBackend):
    """
    Stores files on the local filesystem under a configurable base directory.
    Replace with S3Storage when ready — interface is identical.
    """

    def __init__(self, base_dir: str = "uploads"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, file_obj, key: str) -> str:
        dest = self.base_dir / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            shutil.copyfileobj(file_obj, f)
        return key

    def get_path(self, key: str) -> str:
        return str(self.base_dir / key)

    def delete(self, key: str) -> None:
        target = self.base_dir / key
        if target.exists():
            target.unlink()

    def exists(self, key: str) -> bool:
        return (self.base_dir / key).exists()


# ---------------------------------------------------------------------------
# Future: drop-in S3 replacement
# ---------------------------------------------------------------------------
# class S3Storage(StorageBackend):
#     def __init__(self, bucket: str, prefix: str = ""):
#         import boto3
#         self.bucket = bucket
#         self.prefix = prefix
#         self.client = boto3.client("s3")
#
#     def save(self, file_obj, key: str) -> str:
#         self.client.upload_fileobj(file_obj, self.bucket, f"{self.prefix}{key}")
#         return key
#
#     def get_path(self, key: str) -> str:
#         return f"s3://{self.bucket}/{self.prefix}{key}"
#
#     def delete(self, key: str) -> None:
#         self.client.delete_object(Bucket=self.bucket, Key=f"{self.prefix}{key}")
#
#     def exists(self, key: str) -> bool:
#         try:
#             self.client.head_object(Bucket=self.bucket, Key=f"{self.prefix}{key}")
#             return True
#         except Exception:
#             return False


def get_storage() -> StorageBackend:
    """
    Factory used throughout the app. Switch from local → S3 here only.
    """
    base_dir = os.getenv("UPLOAD_DIR", "uploads")
    return LocalStorage(base_dir=base_dir)
