"""Chunked verified reads keep the exact size limit and hash check (2026-11-20 speed fix)."""
import hashlib

import pytest

import sovereign_workbench.search as search


@pytest.mark.parametrize("chunk", [1, 7, 1024 * 1024])
def test_size_limit_and_hash_are_exact_across_chunk_sizes(tmp_path, monkeypatch, chunk):
    monkeypatch.setattr(search, "READ_CHUNK_BYTES", chunk)
    data = bytes(range(256)) * 4  # 1024 bytes
    (tmp_path / "a.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    assert search._read_verified(str(tmp_path), "a.bin", digest, 1024) == (search.VERIFIED, data)
    assert search._read_verified(str(tmp_path), "a.bin", digest, 1023) == (search.TOO_LARGE, None)
    assert search._read_verified(str(tmp_path), "a.bin", "0" * 64, 1024) == (search.CHANGED, None)
    (tmp_path / "empty.bin").write_bytes(b"")
    empty = hashlib.sha256(b"").hexdigest()
    assert search._read_verified(str(tmp_path), "empty.bin", empty, 1024) == (search.VERIFIED, b"")
