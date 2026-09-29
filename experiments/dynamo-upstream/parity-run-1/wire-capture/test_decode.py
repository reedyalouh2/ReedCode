import copy
import json
from pathlib import Path
import struct
import ipaddress
import msgpack
import tarfile
import unittest
from unittest.mock import patch

import xxhash

from decode import decode, read_pcap, reassemble, sha, two_parts, link_frontend_logs, outer_requests, packet_payload


ROOT = Path(__file__).resolve().parent


class WireTests(unittest.TestCase):
    @staticmethod
    def local_packet(source='172.24.0.2', target='172.24.0.2', source_port=40000, target_port=20000):
        ip = bytearray(20)
        ip[0], ip[9] = 0x45, 6
        ip[2:4] = (43).to_bytes(2, 'big')
        ip[12:16], ip[16:20] = ipaddress.ip_address(source).packed, ipaddress.ip_address(target).packed
        tcp = bytearray(20)
        tcp[:8] = struct.pack('>HHI', source_port, target_port, 100)
        tcp[12] = 0x50
        return b'\0' * 12 + b'\x08\x00' + ip + tcp + b'abc'

    def test_nonloopback_requires_explicit_exact_same_host_address(self):
        packet = self.local_packet()
        with self.assertRaisesRegex(ValueError, 'explicitly allowed'):
            packet_payload(packet, 1)
        key, sequence, _, payload = packet_payload(packet, 1, ['172.24.0.2'])
        self.assertEqual(key, ('172.24.0.2', 40000, '172.24.0.2', 20000))
        self.assertEqual((sequence, payload), (100, b'abc'))
        for source, target, allowed in [('172.24.0.2', '172.24.0.3', ['172.24.0.2', '172.24.0.3']),
                                        ('172.24.0.3', '172.24.0.3', ['172.24.0.2'])]:
            with self.assertRaisesRegex(ValueError, 'explicitly allowed'):
                packet_payload(self.local_packet(source, target), 1, allowed)

    def test_nonloopback_requires_study_port_in_either_direction(self):
        with self.assertRaisesRegex(ValueError, 'explicitly allowed'):
            packet_payload(self.local_packet(target_port=8000), 1, ['172.24.0.2'])
        packet_payload(self.local_packet(source_port=20003, target_port=41000), 1, ['172.24.0.2'])
        with self.assertRaises(ValueError):
            packet_payload(self.local_packet(), 1, ['172.24.0.0/16'])

    def test_allowlist_preserves_loopback_default_and_packet_validation(self):
        packet_payload(self.local_packet('127.0.0.1', '127.0.0.1', target_port=8000), 1)
        with self.assertRaisesRegex(ValueError, 'Truncated IPv4'):
            packet_payload(self.local_packet()[:-1], 1, ['172.24.0.2'])

    @classmethod
    def setUpClass(cls):
        cls.pcaps = {}
        with tarfile.open(ROOT / "evidence.tar.gz") as archive:
            for client in ("codex", "claude"):
                cls.pcaps[client] = archive.extractfile(client + "/runtime.pcap").read()
                callbacks = [json.loads(archive.extractfile(f"{client}/backend-{i}.json").read()) for i in (1, 2)]
                setattr(cls, client + "_callbacks", callbacks)

    def test_actual_stock_wire_matches_all_four_callback_inputs_and_outputs(self):
        for client, raw in self.pcaps.items():
            report = decode(raw)
            self.assertEqual([r["request"] for r in report["requests"]], getattr(self, client + "_callbacks"))
            self.assertEqual([r["output_token_ids"] for r in report["requests"]], [[151668, 198, 151645]] * 2)
            self.assertEqual([r["request_id"] for r in report["requests"]],
                             [r["trace_headers"]["request-id"] for r in report["requests"]])

    def test_reordered_tcp_packets_and_identical_retransmissions_preserve_bytes(self):
        packets = read_pcap(self.pcaps["codex"])
        baseline = {tuple(s["direction"]): s["data"] for s in reassemble(packets)}
        replayed = packets[::-1] + [p for p in packets if p["payload"]]
        self.assertEqual({tuple(s["direction"]): s["data"] for s in reassemble(replayed)}, baseline)

    def test_tcp_sequence_wrap_is_reassembled(self):
        packets = read_pcap(self.pcaps["codex"])
        baseline = {tuple(s["direction"]): s["data"] for s in reassemble(packets)}
        offsets = {p["key"]: (0xfffffff0 - p["sequence"]) & 0xffffffff for p in packets if p["flags"] & 2}
        for packet in packets:
            packet["sequence"] = (packet["sequence"] + offsets[packet["key"]]) & 0xffffffff
        self.assertEqual({tuple(s["direction"]): s["data"] for s in reassemble(packets)}, baseline)

    def test_missing_stream_start_is_rejected(self):
        packets = [p for p in read_pcap(self.pcaps["codex"]) if not p["flags"] & 2]
        with self.assertRaisesRegex(ValueError, "no captured SYN"):
            reassemble(packets)

    def test_a_missing_payload_packet_is_rejected(self):
        packets = read_pcap(self.pcaps["codex"])
        victim = next(i for i, p in enumerate(packets) if len(p["payload"]) > 1000)
        del packets[victim]
        with self.assertRaisesRegex(ValueError, "gap|Missing TCP bytes"):
            reassemble(packets)

    def test_conflicting_retransmission_is_rejected(self):
        packets = read_pcap(self.pcaps["codex"])
        extra = copy.deepcopy(next(p for p in packets if p["payload"]))
        extra["payload"] = bytes([extra["payload"][0] ^ 1]) + extra["payload"][1:]
        with self.assertRaisesRegex(ValueError, "Conflicting retransmitted"):
            reassemble(packets + [extra])

    def test_truncated_packet_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Truncated PCAP"):
            decode(self.pcaps["codex"][:-1])

    def test_empty_failed_capture_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no packets"):
            decode(self.pcaps["codex"][:24])

    def test_debug_frame_checksum_and_incomplete_frame(self):
        body = b'{"example":true}'
        frame = struct.pack(">QQQ", len(body), 0, xxhash.xxh3_64_intdigest(body)) + body
        self.assertEqual(two_parts(frame)[0]["header"], {"example": True})
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            two_parts(frame[:-1] + bytes([frame[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "Truncated"):
            two_parts(frame[:-1])

    def test_observer_header_joins_stock_log_to_wire_without_changing_tokens(self):
        with tarfile.open(ROOT / "evidence.tar.gz") as archive:
            for client in ("codex", "claude"):
                report = decode(archive.extractfile(client + "-ids/runtime.pcap").read())
                raw = archive.extractfile(client + "-ids/frontend.log").read()
                linked = link_frontend_logs(report, [("frontend.log", raw)])
                self.assertEqual(linked["matched"], 2)
                self.assertFalse(linked["unlinked_model_request_ids"])
                baseline = decode(self.pcaps[client])
                self.assertEqual([r["input_token_ids"] for r in report["requests"]],
                                 [r["input_token_ids"] for r in baseline["requests"]])
                self.assertEqual([r["http_link"]["http_request_id"] for r in report["requests"]],
                                 [f"wire-{client}-1", f"wire-{client}-2"])
                with self.assertRaisesRegex(ValueError, "Ambiguous"):
                    link_frontend_logs(report, [("twice", raw + raw)])

    def test_only_reset_control_may_have_an_uncaptured_response(self):
        raw = self.pcaps["codex"]
        normal = decode(raw)
        control_id = normal["requests"][0]["request_id"]
        control_subject = normal["requests"][0]["response_subject"]
        streams = reassemble(read_pcap(raw))
        kept = []
        for stream in streams:
            if stream["data"][:2] == b"\0\0" and any(stream["data"]):
                first = two_parts(stream["data"])[0]["header"]
                if isinstance(first, dict) and first.get("subject") == control_subject:
                    continue
            kept.append(stream)
        def with_reset(data):
            frames = outer_requests(data)
            for frame in frames:
                if frame["header"]["id"] == control_id:
                    frame["endpoint"] = "dynamo.backend/clear_kv_blocks"
            return frames
        with patch("decode.reassemble", return_value=kept):
            with self.assertRaisesRegex(ValueError, "Missing backend"):
                decode(raw)
            with patch("decode.outer_requests", side_effect=with_reset):
                result = decode(raw)
                control = next(r for r in result["requests"] if r["request_id"] == control_id)
                self.assertFalse(control["complete"])
                self.assertIsNone(control["output_token_ids"])
                self.assertEqual(control["response_capture_status"], "control_response_outside_capture")

    def test_named_recovery_rpc_keeps_plain_control_responses(self):
        raw = self.pcaps["codex"]
        first = decode(raw)["requests"][0]
        endpoint_name = "worker_kv_query_source_abc123"

        def as_recovery(data):
            frames = outer_requests(data)
            for frame in frames:
                if frame["header"]["id"] == first["request_id"]:
                    frame["endpoint"] = "worker/" + endpoint_name
                    frame["body"] = (b'{}' if first["payload_codec"] == "json"
                                     else msgpack.packb({}, use_bin_type=True))
            return frames

        def plain_reply(data):
            frames = two_parts(data)
            if frames and isinstance(frames[0]["header"], dict) and frames[0]["header"].get("subject") == first["response_subject"]:
                for frame in frames[1:]:
                    if frame["header"] is None and frame["body"]:
                        codec = first["payload_codec"]
                        wrapper = json.loads(frame["body"]) if codec == "json" else msgpack.unpackb(frame["body"], raw=False)
                        wrapper["data"] = {"recovery": "example"}
                        frame["body"] = (json.dumps(wrapper).encode() if codec == "json"
                                         else msgpack.packb(wrapper, use_bin_type=True))
            return frames

        with patch("decode.outer_requests", side_effect=as_recovery), patch("decode.two_parts", side_effect=plain_reply):
            row = next(r for r in decode(raw)["requests"] if r["request_id"] == first["request_id"])
            self.assertEqual(row["kind"], "control")
            self.assertTrue(row["complete"])
            self.assertEqual(row["input_token_ids"], [])
            self.assertEqual(row["output_token_ids"], [])
            self.assertTrue(row["responses"])
            endpoint_name = "worker_kv_query_source_not_hex"
            with self.assertRaisesRegex(ValueError, 'exact token-ID array'):
                decode(raw)

    def test_frozen_packet_hashes(self):
        manifest = json.loads((ROOT / "evidence.json").read_text())
        raw = (ROOT / "evidence.tar.gz").read_bytes()
        self.assertEqual(sha(raw), manifest["archive_sha256"])
        with tarfile.open(ROOT / "evidence.tar.gz") as archive:
            self.assertEqual(set(archive.getnames()), set(manifest["files"]))
            for name, expected in manifest["files"].items():
                self.assertEqual(sha(archive.extractfile(name).read()), expected)


if __name__ == "__main__":
    unittest.main()
