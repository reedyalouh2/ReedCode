"""Decode plaintext Dynamo TCP traffic from a complete loopback PCAP capture."""

import argparse
from collections import Counter
import hashlib
import ipaddress
import json
import re
from pathlib import Path
import struct

import msgpack
import xxhash


MAX_BYTES = 128 * 1024 * 1024


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def packet_payload(packet, linktype, local_addresses=(), local_ports=(20000, 20003)):
    if linktype == 1:
        if len(packet) < 14:
            raise ValueError("Truncated Ethernet header")
        protocol = int.from_bytes(packet[12:14], "big")
        packet = packet[14:]
    elif linktype == 113:
        if len(packet) < 16:
            raise ValueError("Truncated Linux cooked header")
        protocol = int.from_bytes(packet[14:16], "big")
        packet = packet[16:]
    elif linktype == 276:
        if len(packet) < 20:
            raise ValueError("Truncated Linux cooked v2 header")
        protocol = int.from_bytes(packet[:2], "big")
        packet = packet[20:]
    else:
        raise ValueError(f"Unsupported PCAP linktype {linktype}")
    if protocol == 0x0800:
        if len(packet) < 20 or packet[0] >> 4 != 4:
            raise ValueError("Invalid IPv4 header")
        size, total = (packet[0] & 15) * 4, int.from_bytes(packet[2:4], "big")
        if size < 20 or total < size or total > len(packet):
            raise ValueError("Truncated IPv4 packet")
        if int.from_bytes(packet[6:8], "big") & 0x3fff:
            raise ValueError("Fragmented IP packets require a separate validated reassembler")
        if packet[9] != 6:
            raise ValueError("Capture contains non-TCP IPv4 traffic")
        source, target = map(str, (ipaddress.ip_address(packet[12:16]), ipaddress.ip_address(packet[16:20])))
        packet = packet[size:total]
    elif protocol == 0x86dd:
        if len(packet) < 40 or packet[0] >> 4 != 6:
            raise ValueError("Invalid IPv6 header")
        total = 40 + int.from_bytes(packet[4:6], "big")
        if total > len(packet) or packet[6] != 6:
            raise ValueError("Truncated IPv6 packet or unsupported extension headers")
        source, target = map(str, (ipaddress.ip_address(packet[8:24]), ipaddress.ip_address(packet[24:40])))
        packet = packet[40:total]
    else:
        raise ValueError(f"Unsupported network protocol {protocol}")
    if len(packet) < 20:
        raise ValueError("Truncated TCP header")
    source_port, target_port, sequence = struct.unpack(">HHI", packet[:8])
    loopback = ipaddress.ip_address(source).is_loopback and ipaddress.ip_address(target).is_loopback
    if not loopback:
        allowed = {str(ipaddress.ip_address(value)) for value in local_addresses}
        if source != target or source not in allowed or not {source_port, target_port}.intersection(local_ports):
            raise ValueError("This parser is restricted to loopback or explicitly allowed same-host study traffic")
    offset = (packet[12] >> 4) * 4
    if offset < 20 or offset > len(packet):
        raise ValueError("Invalid TCP data offset")
    return (source, source_port, target, target_port), sequence, packet[13], packet[offset:]


def read_pcap(raw, local_addresses=(), local_ports=(20000, 20003)):
    formats = {b"\xd4\xc3\xb2\xa1": ("<", 1_000_000), b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
               b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000), b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000)}
    if len(raw) < 24 or raw[:4] not in formats:
        raise ValueError("Expected classic PCAP with complete global header")
    endian, scale = formats[raw[:4]]
    major, minor, _, _, snaplen, linktype = struct.unpack(endian + "HHIIII", raw[4:24])
    if (major, minor) != (2, 4) or snaplen == 0:
        raise ValueError("Unsupported PCAP version or snap length")
    offset, packets = 24, []
    while offset < len(raw):
        if len(raw) - offset < 16:
            raise ValueError("Truncated PCAP record header")
        seconds, fraction, captured, original = struct.unpack(endian + "IIII", raw[offset:offset + 16])
        offset += 16
        if captured != original or captured > snaplen or captured > len(raw) - offset:
            raise ValueError("Truncated PCAP packet")
        key, sequence, flags, payload = packet_payload(
            raw[offset:offset + captured], linktype, local_addresses, local_ports)
        packets.append({"key": key, "sequence": sequence, "flags": flags, "payload": payload,
                        "timestamp_ns": seconds * 1_000_000_000 + fraction * (1_000_000_000 // scale)})
        offset += captured
    if not packets:
        raise ValueError("PCAP contains no packets")
    return packets


def reassemble(packets):
    directions = {}
    for packet in packets:
        key = packet["key"]
        state = directions.setdefault(key, {"syn": None, "segments": [], "fin": None, "reset": False})
        seq, flags, payload = packet["sequence"], packet["flags"], packet["payload"]
        if flags & 2:
            if state["syn"] not in (None, seq):
                raise ValueError("TCP four-tuple was reused; split the capture into connection epochs")
            state["syn"] = seq
        if payload:
            state["segments"].append(((seq + bool(flags & 2)) & 0xffffffff, payload, packet["timestamp_ns"]))
        if flags & 1:
            end = (seq + bool(flags & 2) + len(payload)) & 0xffffffff
            if state["fin"] not in (None, end):
                raise ValueError("Conflicting TCP FIN sequence numbers")
            state["fin"] = end
        state["reset"] |= bool(flags & 4)
    streams = []
    for key, state in directions.items():
        if not state["segments"]:
            continue
        if state["syn"] is None:
            raise ValueError("Data stream has no captured SYN; its beginning is unknown")
        base = (state["syn"] + 1) & 0xffffffff
        segments = sorted((((seq - base) & 0xffffffff), data, stamp) for seq, data, stamp in state["segments"])
        result, duplicates = bytearray(), 0
        for offset, data, _ in segments:
            if offset > len(result):
                raise ValueError("TCP byte gap or unsupported sequence epoch")
            overlap = min(len(result) - offset, len(data))
            if result[offset:offset + overlap] != data[:overlap]:
                raise ValueError("Conflicting retransmitted TCP bytes")
            duplicates += overlap
            result.extend(data[overlap:])
            if len(result) > MAX_BYTES:
                raise ValueError("Stream exceeds the bounded study capture limit")
        if state["fin"] is not None and (state["fin"] - base) & 0xffffffff != len(result):
            raise ValueError("Missing TCP bytes before FIN")
        streams.append({"direction": list(key), "data": bytes(result), "duplicate_bytes": duplicates,
                        "first_timestamp_ns": min(row[2] for row in segments),
                        "fin_seen": state["fin"] is not None, "reset_seen": state["reset"]})
    return streams


def two_parts(data):
    frames, offset = [], 0
    while offset < len(data):
        if len(data) - offset < 24:
            raise ValueError("Truncated Dynamo two-part frame header")
        head, body, checksum = struct.unpack(">QQQ", data[offset:offset + 24])
        end = offset + 24 + head + body
        if head + body > MAX_BYTES or end > len(data):
            raise ValueError("Truncated or oversized Dynamo two-part frame")
        raw = data[offset + 24:end]
        if checksum and xxhash.xxh3_64_intdigest(raw) != checksum:
            raise ValueError("Dynamo two-part checksum mismatch")
        frames.append({"header": json.loads(raw[:head]) if head else None,
                       "body": raw[head:], "offset": offset, "bytes": end - offset,
                       "sha256": sha(data[offset:end])})
        offset = end
    return frames


def outer_requests(data):
    rows, offset = [], 0
    while offset < len(data):
        start = offset
        if len(data) - offset < 2:
            raise ValueError("Truncated request endpoint length")
        n = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        if not 0 < n < 1024 or offset + n + 2 > len(data):
            raise ValueError("Invalid request endpoint")
        endpoint = data[offset:offset + n].decode()
        offset += n
        n = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        if offset + n + 4 > len(data):
            raise ValueError("Truncated request headers")
        headers = json.loads(data[offset:offset + n])
        offset += n
        n = int.from_bytes(data[offset:offset + 4], "big")
        offset += 4
        if n > MAX_BYTES or offset + n > len(data):
            raise ValueError("Truncated request payload")
        parts = two_parts(data[offset:offset + n])
        if len(parts) != 1 or not isinstance(parts[0]["header"], dict):
            raise ValueError("Expected one request control/body frame")
        rows.append({"endpoint": endpoint, "trace_headers": headers, **parts[0],
                     "outer_offset": start, "outer_sha256": sha(data[start:offset + n])})
        offset += n
    return rows


def unpack(data, codec):
    if codec == "json":
        return json.loads(data)
    if codec == "msgpack":
        return msgpack.unpackb(data, raw=False, strict_map_key=True)
    raise ValueError("Unsupported request payload codec: " + str(codec))


def token_ids(value):
    if not isinstance(value, list) or any(type(token) is not int or not 0 <= token <= 0xffffffff for token in value):
        raise ValueError("Expected an exact token-ID array")
    return value


def decode(raw, local_addresses=(), local_ports=(20000, 20003)):
    streams = reassemble(read_pcap(raw, local_addresses, local_ports))
    requests, response_streams, acknowledgements, controls = {}, [], 0, []
    for stream in streams:
        data = stream["data"]
        meta = {k: v for k, v in stream.items() if k != "data"}
        if len(data) >= 2 and data[:2] != b"\0\0":
            for frame in outer_requests(data):
                control = frame["header"]
                if control.get("request_type") != "single_in":
                    raise ValueError("Only unary Dynamo requests are validated by this parser")
                request_id = control["id"]
                if request_id in requests:
                    raise ValueError("Duplicate Dynamo request ID")
                if frame["trace_headers"].get("request-id") != request_id:
                    raise ValueError("Transport header and request control IDs disagree")
                codec = control.get("payload_codec", "json")
                body = unpack(frame["body"], codec)
                endpoint_name = frame["endpoint"].rsplit("/", 1)[-1]
                recovery = re.fullmatch(r"worker_kv_query_source_[0-9a-f]+", endpoint_name)
                kind = "control" if endpoint_name == "clear_kv_blocks" or recovery else "model"
                ids = token_ids(body.get("token_ids")) if kind == "model" else []
                connection = control["connection_info"]
                if connection["transport"] != "tcp_server":
                    raise ValueError("Expected a TCP response stream")
                info = json.loads(connection["info"])
                if info["context"] != request_id or info["stream_type"] != "response":
                    raise ValueError("Response subject is not bound to the request context")
                requests[request_id] = {"request_id": request_id, "response_subject": info["subject"],
                    "kind": kind,
                    "payload_codec": codec, "endpoint": frame["endpoint"], "control": control,
                    "trace_headers": frame["trace_headers"], "request": body,
                    "input_token_ids": ids, "input_tokens_sha256": sha(json_bytes(ids)),
                    "wire_payload_sha256": sha(frame["body"]), "outer_sha256": frame["outer_sha256"],
                    "capture": meta, "responses": []}
        elif data == b"\0\0\0\0" * (len(data) // 4):
            acknowledgements += len(data) // 4
        else:
            frames = two_parts(data)
            first = frames[0]["header"] if frames else None
            if isinstance(first, dict) and first.get("stream_type") == "response" and isinstance(first.get("subject"), str):
                if frames[0]["body"]:
                    raise ValueError("Response handshake unexpectedly has a body")
                response_streams.append((first["subject"], frames[1:], meta))
            elif all(frame["header"] in ("sentinel", "stop", "kill") and not frame["body"] for frame in frames):
                controls.append({"capture": meta, "controls": [f["header"] for f in frames]})
            else:
                raise ValueError("Unrecognized TCP stream; no traffic was silently discarded")
    by_subject = {row["response_subject"]: row for row in requests.values()}
    if len(by_subject) != len(requests):
        raise ValueError("Ambiguous response subject")
    seen = set()
    for subject, frames, meta in response_streams:
        if subject not in by_subject or subject in seen:
            raise ValueError("Unmatched or duplicate response stream")
        seen.add(subject)
        row = by_subject[subject]
        complete, prologue, sentinel = False, False, False
        output = []
        for frame in frames:
            header, body = frame["header"], frame["body"]
            if isinstance(header, dict) and "error" in header and not body:
                if header["error"] is not None or prologue:
                    raise ValueError("Failed or duplicate response prologue")
                prologue = True
            elif header == "sentinel" and not body:
                sentinel = True
            elif header is None and body:
                if not prologue or complete or sentinel:
                    raise ValueError("Response payload lies outside its open stream")
                wrapper = unpack(body, row["payload_codec"])
                if type(wrapper.get("complete_final")) is not bool:
                    raise ValueError("Response completion marker is unavailable")
                annotated = wrapper.get("data")
                if annotated is not None and row["kind"] == "model":
                    if not isinstance(annotated, dict) or "data" not in annotated:
                        raise ValueError("Expected Dynamo Annotated response data")
                    payload = annotated["data"]
                    if payload is not None and "token_ids" in payload:
                        output.extend(token_ids(payload["token_ids"]))
                complete = wrapper["complete_final"]
                row["responses"].append({"wrapper": wrapper, "frame_sha256": frame["sha256"]})
            else:
                raise ValueError("Unrecognized or interrupted response frame")
        if not prologue or not complete or not sentinel:
            raise ValueError("Response stream is incomplete")
        row.update(output_token_ids=output, output_tokens_sha256=sha(json_bytes(output)),
                   response_capture=meta, complete=True)
    required = {subject for subject, row in by_subject.items() if row["kind"] == "model"}
    if not required or not required.issubset(seen):
        raise ValueError("Missing backend requests or matched response streams")
    for subject in set(by_subject) - seen:
        by_subject[subject].update(complete=False, output_token_ids=None,
                                  response_capture_status="control_response_outside_capture")
    return {"pcap_sha256": sha(raw), "local_address_allowlist": sorted(local_addresses),
            "local_port_allowlist": sorted(local_ports),
            "requests": sorted(requests.values(), key=lambda r: r["capture"]["first_timestamp_ns"]),
            "data_streams": len(streams), "transport_acknowledgements": acknowledgements, "controls": controls}


def link_frontend_logs(report, logs):
    """Join the public observer header through unchanged frontend JSON logs."""
    by_internal, by_observer = {}, {}
    sources = {}
    for name, raw in logs:
        sources[name] = sha(raw)
        for line_number, line in enumerate(raw.splitlines(), 1):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if row.get("message") != "request received" or not row.get("x_request_id"):
                continue
            internal, observer = row.get("request_id"), row["x_request_id"]
            if not isinstance(internal, str) or not isinstance(observer, str):
                raise ValueError("Invalid request linkage in frontend log")
            if internal in by_internal or observer in by_observer:
                raise ValueError("Ambiguous request linkage in frontend logs")
            bridge = {"http_request_id": observer, "runtime_request_id": internal,
                      "frontend_log": name, "line": line_number, "row": row}
            by_internal[internal] = bridge
            by_observer[observer] = bridge
    matched = 0
    for request in report["requests"]:
        if request["request_id"] in by_internal:
            request["http_link"] = by_internal[request["request_id"]]
            matched += 1
    report["frontend_log_linkage"] = {"matched": matched, "files": sources,
        "unlinked_model_request_ids": [r["request_id"] for r in report["requests"]
                                       if r["kind"] == "model" and "http_link" not in r]}
    return report["frontend_log_linkage"]


def compare_callbacks(report, directory):
    expected = [json.loads(p.read_text()) for p in sorted(directory.glob("backend-[0-9]*.json"))]
    actual = [row["request"] for row in report["requests"]]
    if not expected or Counter(sha(json_bytes(row)) for row in expected) != Counter(sha(json_bytes(row)) for row in actual):
        raise ValueError("Wire requests differ from the CPU worker's received callback payloads")
    for row in report["requests"]:
        if row["output_token_ids"] != [151668, 198, 151645]:
            raise ValueError("Wire output differs from the CPU worker's fixed response")
    return {"callback_requests_matched": len(expected), "fixed_output_tokens_matched": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--callbacks", type=Path)
    parser.add_argument("--frontend-log", type=Path, action="append", default=[])
    parser.add_argument("--local-address", action="append", default=[],
                        help="Observed address of this host; allows only src=dst traffic on ports 20000/20003")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = decode(args.pcap.read_bytes(), local_addresses=args.local_address)
    if args.frontend_log:
        link_frontend_logs(report, [(str(p), p.read_bytes()) for p in args.frontend_log])
    if args.callbacks:
        report["cpu_callback_check"] = compare_callbacks(report, args.callbacks)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"requests": len(report["requests"]), "pcap_sha256": report["pcap_sha256"],
                      "cpu_callback_check": report.get("cpu_callback_check")}, indent=2))
