"""Bounded byte-exact reconstruction of judge-side document slices."""
import base64
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import re

from .protocol import integer


@dataclass
class DocumentLedger:
    files: OrderedDict = field(default_factory=OrderedDict)
    max_file_bytes: int = 131072
    max_total_bytes: int = 524288

    def ingest(self, data, evidence_id):
        path, digest = data.get("path"), data.get("file_sha256")
        offset, end, size = data.get("offset"), data.get("next_byte"), data.get("file_size")
        encoded, chunk_hash = data.get("bytes_base64"), data.get("chunk_sha256")
        if not isinstance(path, str) or not integer(offset, 0) or not integer(end, offset) or not integer(size, end):
            raise ValueError("invalid slice bounds")
        if not isinstance(encoded, str) or len(encoded) > 11000 or not isinstance(chunk_hash, str):
            raise ValueError("invalid encoded slice")
        try:
            block = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("invalid slice encoding") from exc
        if len(block) > 8192 or end-offset != len(block) or hashlib.sha256(block).hexdigest() != chunk_hash:
            raise ValueError("slice length/hash mismatch")
        if type(data.get("has_more")) is not bool or data["has_more"] != (end < size):
            raise ValueError("inconsistent end-of-file marker")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("invalid document hash")
        result = {k: v for k, v in data.items() if k != "bytes_base64"}
        result["completeness"] = "slice"
        try:
            result["text"] = block.decode("utf-8")
            result["utf8_complete"] = True
        except UnicodeDecodeError:
            result["text"], result["utf8_complete"] = None, False
        result["slice_text"] = result["text"]
        result["slice_offset"] = offset
        result["slice_next_byte"] = end
        if digest is None or size > self.max_file_bytes:
            result["next_missing_byte"] = end
            result["assembly_status"] = "whole_file_exceeds_local_verification_budget"
            return result
        previous = self.files.get(path)
        if previous is None or previous["hash"] != digest or previous["size"] != size:
            previous = {"hash": digest, "size": size, "chunks": {}}
            self.files[path] = previous
        self.files.move_to_end(path)
        for start, (old, _) in previous["chunks"].items():
            lo, hi = max(start, offset), min(start+len(old), end)
            if lo < hi and old[lo-start:hi-start] != block[lo-offset:hi-offset]:
                self.files.pop(path, None)
                raise ValueError("conflicting bytes for same file fingerprint")
        previous["chunks"][offset] = (block, evidence_id)
        pieces, position, references = [], 0, []
        for start, (chunk, source) in sorted(previous["chunks"].items()):
            if start > position:
                break
            if start+len(chunk) > position:
                pieces.append(chunk[position-start:])
                position = start+len(chunk)
                references.append(source)
        result["next_missing_byte"] = position
        if position == size:
            whole = b"".join(pieces)
            if hashlib.sha256(whole).hexdigest() != digest:
                self.files.pop(path, None)
                raise ValueError("assembled bytes do not match whole-file fingerprint")
            try:
                result["text"] = whole.decode("utf-8")
                result["utf8_complete"] = True
            except UnicodeDecodeError:
                result["text"], result["utf8_complete"] = None, False
            result.update(completeness="complete", offset=0, next_byte=size, has_more=False,
                          assembly_status="whole_file_hash_verified", source_evidence_ids=references)
        else:
            result["assembly_status"] = "incomplete"
        def stored_bytes():
            return sum(len(chunk) for item in self.files.values() for chunk, _ in item["chunks"].values())
        while len(self.files) > 8 or stored_bytes() > self.max_total_bytes:
            self.files.popitem(last=False)
        return result
